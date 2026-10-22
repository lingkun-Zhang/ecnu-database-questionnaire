"""Disposable browser fixture for copying V1, editing/saving, then publishing V3.

Run the real built frontend/API on an ephemeral loopback port and random MongoDB
database. Audit selected real HTTP writes without changing requests or responses.
Create stop.request in the new output directory to shut down and drop only this
runner's generated database. No browser automation or production reset occurs here.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import re
import secrets
import socket
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "backend"), str(ROOT)]

import httpx
import uvicorn
from app.config import Settings
from app.main import create_app

USERNAME = "history_copy_browser"
PASSWORD = "HistoryBrowser2026!"  # Disposable local fixture, never a real account.
V3_TITLE = "历史恢复验收 V3（源自V1）"


def definition(number):
    maximum = 120 if number == 1 else 100
    return {"title": f"历史恢复验收 V{number}", "description": "独立测试：历史复制后仍保留各版含义。",
            "allowAnonymous": True, "entryQuestionId": "q_number", "questions": [
        {"id": "q_number", "kind": "number", "title": f"请输入0至{maximum}的数字", "required": True,
         "config": {"min": 0, "max": maximum, "integerOnly": True}, "defaultTarget": "q_note", "rules": []},
        {"id": "q_note", "kind": "text", "title": "备注（选填）", "required": False,
         "config": {"minLength": 1, "maxLength": 100}, "defaultTarget": "END", "rules": []}]}


async def snapshot(db):
    return {name: await db[name].find({}).sort("_id", 1).to_list(length=None)
            for name in ("users", "surveys", "survey_versions", "responses")}


def summary(documents):
    survey = documents["surveys"][0]
    versions = sorted(documents["survey_versions"], key=lambda item: item["versionNo"])
    current = next(item for item in versions if item["_id"] == survey["currentVersionId"])
    return {"draftRevision": survey["draftRevision"], "draftTitle": survey["draft"]["title"],
            "draftMax": survey["draft"]["questions"][0]["config"]["max"],
            "currentVersionNo": current["versionNo"], "currentTitle": current["definition"]["title"],
            "currentMax": current["definition"]["questions"][0]["config"]["max"],
            "settingsRevision": survey["settingsRevision"], "submitSequence": survey["submitSequence"],
            "policyLocked": survey["policyLocked"], "versionNumbers": [item["versionNo"] for item in versions],
            "versions": [{"versionNo": version["versionNo"], "sourceDraftRevision": version["sourceDraftRevision"],
                          "title": version["definition"]["title"],
                          "max": version["definition"]["questions"][0]["config"]["max"],
                          "responses": len(rows := [row for row in documents["responses"] if row["versionId"] == version["_id"]]),
                          "numberValues": [answer["value"] for row in rows for answer in row["answers"]
                                           if answer["questionId"] == "q_number"],
                          "anonymousResponses": sum(row["anonymous"] for row in rows)} for version in versions]}


class HistoryAudit:
    def __init__(self, app, output):
        self.app, self.output = app, output
        self.state = {"events": [], "cleanedUp": False}
        self.baseline = self.previous = None

    def save(self):
        temporary = self.output / "state.tmp"
        temporary.write_text(json.dumps(self.state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(self.output / "state.json")

    async def observe(self, method, request, status):
        after = await snapshot(self.app.state.db)
        original = self.baseline
        survey = after["surveys"][0]
        previous_survey = self.previous["surveys"][0]
        phase = ["copy-v1", "save-edited-copy", "publish-v3"][len(self.state["events"])]
        wanted = definition(1)
        if phase != "copy-v1":
            wanted["title"] = V3_TITLE
        checks = {"usersUnchanged": after["users"] == original["users"],
                  "responsesUnchanged": after["responses"] == original["responses"],
                  "originalVersionsUnchanged": all(item in after["survey_versions"] for item in original["survey_versions"]),
                  "requestRevisionMatches": request.get("expectedDraftRevision") == previous_survey["draftRevision"],
                  "draftMatchesPlannedDefinition": survey["draft"] == wanted}
        expected = deepcopy(self.previous)
        target = expected["surveys"][0]
        if phase != "publish-v3":
            target.update(draft=wanted, draftRevision=previous_survey["draftRevision"] + 1,
                          updatedAt=survey["updatedAt"])
            checks.update(statusCorrect=status == 200, methodCorrect=method == "PUT")
        else:
            third = next(item for item in after["survey_versions"] if item["versionNo"] == 3)
            checks.update(statusCorrect=status == 201, methodCorrect=method == "POST",
                          requestCurrentMatches=False)
        # Keep the expected changes explicit; every other persisted field must match.
        if phase == "publish-v3":
            checks["requestCurrentMatches"] = request.get("expectedCurrentVersionId") == previous_survey["currentVersionId"]
            checks["newDefinitionMatches"] = third["definition"] == wanted
            checks["newSourceRevisionMatches"] = third["sourceDraftRevision"] == 4
            checks["newKeyMatchesRequest"] = third["publishKey"] == request.get("publishKey")
            target.update(currentVersionId=third["_id"], lastVersionNo=3, updatedAt=survey["updatedAt"])
            expected["survey_versions"].append(third)
            expected["survey_versions"].sort(key=lambda item: item["_id"])
        checks["onlyExpectedDocumentsChanged"] = after == expected
        self.state["events"].append({"phase": phase, "method": method, "httpStatus": status,
                                     "checks": checks, "snapshot": summary(after)})
        self.previous = after
        self.state["allObservedChecksPassed"] = all(all(event["checks"].values()) for event in self.state["events"])
        self.save()

    async def __call__(self, scope, receive, send):
        paths = {self.state.get("managementPath", "") + suffix for suffix in ("/draft", "/publish")}
        if scope["type"] != "http" or scope.get("method") not in ("PUT", "POST") or scope.get("path") not in paths:
            return await self.app(scope, receive, send)
        body, messages = bytearray(), []

        async def capture_receive():
            message = await receive()
            if message["type"] == "http.request":
                body.extend(message.get("body", b""))
            return message

        async def capture_send(message):
            messages.append(message)

        await self.app(scope, capture_receive, capture_send)
        status = next(message["status"] for message in messages if message["type"] == "http.response.start")
        try:
            await self.observe(scope["method"], json.loads(body), status)
        except Exception as error:
            self.state["auditErrorType"] = type(error).__name__
            self.save()
        # Forward the real service response unchanged, including failures.
        for message in messages:
            await send(message)


async def run(output):
    output.mkdir(parents=True, exist_ok=False)
    database = "survey_test_" + uuid4().hex
    settings = replace(Settings.from_env(), database=database, testing=True,
                       auth_secret=secrets.token_hex(32), fill_secret=secrets.token_hex(32),
                       participant_secret=secrets.token_hex(32))
    app = create_app(settings)
    audit = HistoryAudit(app, output)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    origin = f"http://127.0.0.1:{listener.getsockname()[1]}"
    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application):
        async with original_lifespan(application):
            try:
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://fixture") as client:
                    result = await client.post("/api/auth/register", json={"username": USERNAME, "password": PASSWORD})
                    assert result.status_code == 201
                    headers = {"Authorization": "Bearer " + result.json()["accessToken"]}
                    result = await client.post("/api/surveys", headers=headers, json={"title": definition(1)["title"]})
                    assert result.status_code == 201
                    survey = result.json()
                    result = await client.patch(f'/api/surveys/{survey["id"]}/settings', headers=headers,
                        json={"expectedSettingsRevision": 0, "allowMultiple": True})
                    assert result.status_code == 200
                    previous_version = None
                    for number, value in ((1, 110), (2, 90)):
                        saved = await client.put(f'/api/surveys/{survey["id"]}/draft', headers=headers,
                            json={"expectedDraftRevision": number - 1, "definition": definition(number)})
                        assert saved.status_code == 200
                        version = await client.post(f'/api/surveys/{survey["id"]}/publish', headers=headers,
                            json={"expectedDraftRevision": number, "expectedCurrentVersionId": previous_version,
                                  "publishKey": str(uuid4())})
                        assert version.status_code == 201
                        previous_version = version.json()["id"]
                        attempt = await client.post(f'/api/survey/{survey["publicId"]}/start', headers=headers)
                        assert attempt.status_code == 200
                        response = await client.post(f'/api/survey/{survey["publicId"]}/submit',
                            headers={**headers, "X-Fill-Token": attempt.json()["fillToken"], "Idempotency-Key": str(uuid4())},
                            json={"versionId": previous_version, "anonymous": number == 1,
                                  "answers": {"q_number": value, "q_note": f"原V{number}答卷"}})
                        assert response.status_code == 201
                audit.baseline = audit.previous = await snapshot(application.state.db)
                audit.state.update(origin=origin, username=USERNAME, fillURL=f'{origin}/survey/{survey["publicId"]}',
                                   managementPath=f'/api/surveys/{survey["id"]}', baseline=summary(audit.baseline))
                audit.save()
                print("HISTORY_BROWSER_READY " + json.dumps({"origin": origin, "username": USERNAME,
                                                             "stateFile": str(output / "state.json")}, ensure_ascii=False), flush=True)
                yield
            finally:
                if audit.previous is not None:
                    audit.state["finalSnapshotEqualsLastObservedWrite"] = await snapshot(application.state.db) == audit.previous
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
