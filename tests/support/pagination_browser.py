"""Real browser fixture: 45 mixed responses, page size 20, question filters.

Only the browser drives owner reads. This runner seeds through real APIs, audits
their unchanged responses, and optionally appends response 46 on append.request.
Create stop.request for graceful shutdown and exact generated-database cleanup.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
import json
from pathlib import Path
import re
import secrets
import socket
import sys
from urllib.parse import parse_qs
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "backend"), str(ROOT)]

import httpx
import uvicorn
from app.config import Settings
from app.main import create_app

USERNAME = "pagination_browser"
PASSWORD = "PaginationBrowser2026!"  # Disposable local fixture account only.
COLLECTIONS = ("users", "surveys", "survey_versions", "responses")
PRIVATE = {"identity", "participantKey", "userId", "usernameAtSubmit",
           "submissionKey", "payloadDigest", "oneTimeParticipantKey"}


def definition():
    return {"title": "筛选与分页验收（45份）", "description": "独立数据：奇数匿名、偶数实名；前9份跳过备注。",
            "allowAnonymous": True, "entryQuestionId": "q_number", "questions": [
        {"id": "q_number", "kind": "number", "title": "答卷序号（1—46）", "required": True,
         "config": {"min": 1, "max": 46, "integerOnly": True}, "defaultTarget": "q_note",
         "rules": [{"id": "r_skip", "priority": 10, "condition": {"operator": "lt", "value": 10}, "target": "END"}]},
        {"id": "q_note", "kind": "text", "title": "备注（选填，部分跳过）", "required": False,
         "config": {"minLength": 1, "maxLength": 100}, "defaultTarget": "q_empty", "rules": []},
        {"id": "q_empty", "kind": "text", "title": "留空题（全部未答）", "required": False,
         "config": {"minLength": 1, "maxLength": 100}, "defaultTarget": "END", "rules": []}]}


def row(number):
    answers = {"q_number": number}
    if number >= 10 and number % 3:
        answers["q_note"] = f"NOTE-{number:03}" if number < 46 else "LATE-046"
    return {"acceptedSeq": number, "anonymous": bool(number % 2), "answers": answers}


async def snapshot(db):
    return {name: await db[name].find({}).sort("_id", 1).to_list(length=None) for name in COLLECTIONS}


def keys_in(value):
    if isinstance(value, dict):
        return set(value).union(*(keys_in(item) for item in value.values()))
    if isinstance(value, list):
        return set().union(*(keys_in(item) for item in value))
    return set()


class ReadAudit:
    def __init__(self, app, output):
        self.app, self.output = app, output
        self.rows, self.baseline = [], None
        self.state = {"cleanedUp": False, "events": [], "appendCompleted": False}

    def save(self):
        temporary = self.output / "state.tmp"
        temporary.write_text(json.dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.output / "state.json")

    async def observe(self, scope, status, value):
        query = {key: values[-1] for key, values in parse_qs(scope["query_string"].decode()).items()}
        bound = int(query.get("asOfSeq", len(self.rows)))
        mode, question = query.get("respondentType", "all"), query.get("questionId")
        cohort = [item for item in self.rows if item["acceptedSeq"] <= bound and
                  (mode == "all" or item["anonymous"] == (mode == "anonymous"))]
        checks = {"http200": status == 200, "versionMatches": query.get("versionId") == self.state["versionId"],
                  "boundaryMatches": value["asOfSeq"] == bound, "modeMatches": value["respondentType"] == mode,
                  "privateFieldsAbsent": not PRIVATE.intersection(keys_in(value)),
                  "readDidNotChangeDocuments": await snapshot(self.app.state.db) == self.baseline}
        event = {"endpoint": scope["path"].rsplit("/", 1)[-1], "query": query, "httpStatus": status, "checks": checks}
        if event["endpoint"] == "stats":
            checks["totalsMatch"] = (value["totalResponses"] == len(cohort) and
                value["uniqueParticipants"] == int(bool(cohort)) and
                value["anonymousResponses"] == sum(item["anonymous"] for item in cohort))
            expected = {}
            for qid in ("q_number", "q_note", "q_empty"):
                reached = len(cohort) if qid == "q_number" else sum(item["acceptedSeq"] >= 10 for item in cohort)
                answered = [item["answers"][qid] for item in cohort if qid in item["answers"]]
                expected[qid] = {"A": len(answered), "V": reached, "U": reached - len(answered), "S": len(cohort) - reached}
                if qid == "q_number":
                    expected[qid].update(sum=sum(answered), avg=sum(answered) / len(answered) if answered else None)
            checks["allQuestionMetricsMatch"] = len(value["questions"]) == 3 and all(
                all(actual.get(key) == expected[actual["questionId"]][key] for key in expected[actual["questionId"]])
                for actual in value["questions"])
            event.update(total=value["totalResponses"], expectedQuestionMetrics=expected)
        else:
            cursor, limit = int(query.get("cursor", bound + 1)), int(query.get("limit", 30))
            candidates = sorted([item for item in cohort if item["acceptedSeq"] < cursor and
                                 (not question or question in item["answers"])], key=lambda item: -item["acceptedSeq"])
            expected = candidates[:limit]
            checks["sequencesAndReceiptsMatch"] = [(item["acceptedSeq"], item["receiptId"]) for item in value["items"]] == [
                (item["acceptedSeq"], item["receiptId"]) for item in expected]
            checks["answersAndIdentityMatch"] = all(
                actual["anonymous"] == wanted["anonymous"] and
                {answer["questionId"]: answer["value"] for answer in actual["answers"]} == wanted["answers"] and
                ("respondentName" not in actual if wanted["anonymous"] else actual.get("respondentName") == USERNAME)
                for actual, wanted in zip(value["items"], expected, strict=True))
            checks["nextCursorMatches"] = value["nextCursor"] == (expected[-1]["acceptedSeq"] if len(candidates) > limit else None)
            event.update(sequences=[item["acceptedSeq"] for item in value["items"]],
                         receipts=[item["receiptId"] for item in value["items"]], nextCursor=value["nextCursor"])
        self.state["events"].append(event)
        self.state["allObservedChecksPassed"] = all(all(item["checks"].values()) for item in self.state["events"])
        self.save()

    async def __call__(self, scope, receive, send):
        prefix = self.state.get("managementPath", "")
        if scope["type"] != "http" or scope.get("method") != "GET" or scope.get("path") not in {
                prefix + "/stats", prefix + "/responses"}:
            return await self.app(scope, receive, send)
        messages = []

        async def capture_send(message):
            messages.append(message)

        await self.app(scope, receive, capture_send)
        try:
            status = next(message["status"] for message in messages if message["type"] == "http.response.start")
            body = b"".join(message.get("body", b"") for message in messages if message["type"] == "http.response.body")
            await self.observe(scope, status, json.loads(body))
        except Exception as error:
            self.state["auditErrorType"] = type(error).__name__
            self.save()
        for message in messages:
            await send(message)


async def run(output):
    output.mkdir(parents=True, exist_ok=False)
    database = "survey_test_" + uuid4().hex
    settings = replace(Settings.from_env(), database=database, testing=True,
                       auth_secret=secrets.token_hex(32), fill_secret=secrets.token_hex(32), participant_secret=secrets.token_hex(32))
    app = create_app(settings)
    audit = ReadAudit(app, output)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    origin = f"http://127.0.0.1:{listener.getsockname()[1]}"
    original_lifespan = app.router.lifespan_context
    append_task = None

    @asynccontextmanager
    async def lifespan(application):
        nonlocal append_task
        async with original_lifespan(application):
            try:
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://fixture") as client:
                    result = await client.post("/api/auth/register", json={"username": USERNAME, "password": PASSWORD})
                    assert result.status_code == 201
                    headers = {"Authorization": "Bearer " + result.json()["accessToken"]}
                    result = await client.post("/api/surveys", headers=headers, json={"title": definition()["title"]})
                    assert result.status_code == 201
                    survey = result.json()
                    path = f'/api/surveys/{survey["id"]}'
                    result = await client.patch(path + "/settings", headers=headers,
                        json={"expectedSettingsRevision": 0, "allowMultiple": True})
                    assert result.status_code == 200
                    result = await client.put(path + "/draft", headers=headers,
                        json={"expectedDraftRevision": 0, "definition": definition()})
                    assert result.status_code == 200
                    result = await client.post(path + "/publish", headers=headers,
                        json={"expectedDraftRevision": 1, "expectedCurrentVersionId": None, "publishKey": str(uuid4())})
                    assert result.status_code == 201
                    version = result.json()
                    attempt = await client.post(f'/api/survey/{survey["publicId"]}/start', headers=headers)
                    assert attempt.status_code == 200
                    fill_headers = {**headers, "X-Fill-Token": attempt.json()["fillToken"]}

                    async def submit_number(number):
                        wanted = row(number)
                        result = await client.post(f'/api/survey/{survey["publicId"]}/submit',
                            headers={**fill_headers, "Idempotency-Key": str(uuid4())},
                            json={"versionId": version["id"], "anonymous": wanted["anonymous"], "answers": wanted["answers"]})
                        assert result.status_code == 201
                        wanted["receiptId"] = result.json()["receiptId"]
                        audit.rows.append(wanted)

                    for number in range(1, 46):
                        await submit_number(number)
                    audit.baseline = await snapshot(application.state.db)
                    audit.state.update(origin=origin, username=USERNAME, managementPath=path, versionId=version["id"],
                                       baselineCounts={name: len(items) for name, items in audit.baseline.items()}, seededResponses=45)
                    audit.save()

                    async def append_watcher():
                        while not audit.state["appendCompleted"]:
                            if (output / "append.request").exists():
                                before = audit.baseline
                                await submit_number(46)
                                after = await snapshot(application.state.db)
                                previous_survey, current_survey = before["surveys"][0], after["surveys"][0]
                                audit.state["appendChecks"] = {
                                    "usersAndVersionsUnchanged": before["users"] == after["users"] and before["survey_versions"] == after["survey_versions"],
                                    "onlySequenceAndUpdateTimeChangedInSurvey": current_survey == {
                                        **previous_survey, "submitSequence": 46, "updatedAt": current_survey["updatedAt"]},
                                    "originalResponsesUnchanged": all(item in after["responses"] for item in before["responses"]),
                                    "exactlyOneNewResponse": len(after["responses"]) == 46 and current_survey["submitSequence"] == 46}
                                audit.baseline = after
                                audit.state["appendCompleted"] = True
                                audit.save()
                                return
                            await asyncio.sleep(.25)

                    append_task = asyncio.create_task(append_watcher())
                    print("PAGINATION_BROWSER_READY " + json.dumps({"origin": origin, "username": USERNAME,
                        "stateFile": str(output / "state.json")}, ensure_ascii=False), flush=True)
                    yield
            finally:
                if append_task:
                    append_task.cancel()
                    await asyncio.gather(append_task, return_exceptions=True)
                if audit.baseline is not None:
                    audit.state["finalSnapshotMatches"] = await snapshot(application.state.db) == audit.baseline
                assert re.fullmatch(r"survey_test_[0-9a-f]{32}", database) and settings.database == database
                await application.state.db.client.drop_database(database)
                audit.state["cleanedUp"] = True
                audit.state["databaseAbsentAfterCleanup"] = database not in await application.state.db.client.list_database_names()
                audit.save()

    app.router.lifespan_context = lifespan
    server = uvicorn.Server(uvicorn.Config(audit, log_level="warning", access_log=False))

    async def stop_watcher():
        while not server.should_exit:
            if (output / "stop.request").exists():
                server.should_exit = True
                return
            await asyncio.sleep(.25)

    watcher = asyncio.create_task(stop_watcher())
    try:
        await server.serve(sockets=[listener])
    finally:
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)
        listener.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="A new private output directory")
    args = parser.parse_args()
    asyncio.run(run(args.output.resolve()))
