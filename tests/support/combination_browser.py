"""Disposable real UI fixture for nested condition edits and draft persistence.

Seed only a three-question draft. The browser creates the rules and previews.
Audit real HTTP bodies/responses without replacing them. stop.request gracefully
shuts down this runner and drops only its exact generated database.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import replace
import json
import hashlib
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
from bson.json_util import CANONICAL_JSON_OPTIONS, dumps
from app.config import Settings
from app.main import create_app

USERNAME = "combination_browser"
PASSWORD = "CombinationBrowser2026!"  # Disposable fixture, never a real account.
CONDITION = {"allOf": [{"operator": "gte", "value": 18},
                       {"anyOf": [{"operator": "lte", "value": 60}, {"operator": "eq", "value": 80}]}]}


def definition():
    return {"title": "组合规则页面验收", "description": "在页面编辑：大于等于18，且（小于等于60或等于80）；30优先走默认分支。",
            "allowAnonymous": True, "entryQuestionId": "q_number", "questions": [
        {"id": "q_number", "kind": "number", "title": "测试数字（0—100）", "required": True,
         "config": {"min": 0, "max": 100, "integerOnly": True}, "defaultTarget": "q_default", "rules": []},
        {"id": "q_match", "kind": "text", "title": "命中组合条件", "required": False,
         "config": {"minLength": 1, "maxLength": 100}, "defaultTarget": "END", "rules": []},
        {"id": "q_default", "kind": "text", "title": "默认分支", "required": False,
         "config": {"minLength": 1, "maxLength": 100}, "defaultTarget": "END", "rules": []}]}


async def snapshot(db):
    return {name: await db[name].find({}).sort("_id", 1).to_list(length=None)
            for name in ("users", "surveys", "survey_versions", "responses")}


def fingerprint(documents):
    encoded = dumps(documents, json_options=CANONICAL_JSON_OPTIONS, sort_keys=True,
                    ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class CombinationAudit:
    def __init__(self, app, output):
        self.app, self.output, self.baseline = app, output, None
        self.state = {"events": [], "cleanedUp": False}

    def save(self):
        temporary = self.output / "state.tmp"
        temporary.write_text(json.dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.output / "state.json")

    async def observe(self, scope, request, status, response):
        after = await snapshot(self.app.state.db)
        actual = request["definition"]
        rules = actual["questions"][0]["rules"]
        wanted = definition()
        if len(rules) == 2:
            wanted["questions"][0]["rules"] = [
                {"id": rules[0]["id"], "priority": 10, "condition": CONDITION, "target": "q_match"},
                {"id": rules[1]["id"], "priority": 5, "condition": {"operator": "eq", "value": 30}, "target": "q_default"}]
        checks = {"definitionMatchesPlan": len(rules) == 2 and actual == wanted,
                  "stableUniqueRuleIds": len(rules) == 2 and len({rule["id"] for rule in rules}) == 2}
        event = {"endpoint": scope["path"].rsplit("/", 1)[-1], "httpStatus": status, "checks": checks}
        if event["endpoint"] == "draft":
            previous = self.baseline["surveys"][0]
            expected = deepcopy(self.baseline)
            current = after["surveys"][0]
            expected["surveys"][0].update(draft=wanted, draftRevision=previous["draftRevision"] + 1,
                                          updatedAt=current["updatedAt"])
            checks.update(http200=status == 200, revisionMatches=request["expectedDraftRevision"] == previous["draftRevision"],
                          onlyPlannedDraftChanged=after == expected,
                          responseMatches=response["draft"] == wanted and response["draftRevision"] == current["draftRevision"])
            self.baseline = after
            self.state["lastSnapshotSha256"] = fingerprint(after)
            self.state["savedDefinition"] = actual
            self.state["savedRevision"] = current["draftRevision"]
        else:
            number = request["answers"].get("q_number")
            checks.update(http200=status == 200, previewDidNotWrite=after == self.baseline)
            if number is None:
                checks["emptyInputBlocked"] = response["path"] == ["q_number"] and response["blockedAt"] == "q_number" and not response["complete"]
            else:
                match = number != 30 and number >= 18 and (number <= 60 or number == 80)
                target = "q_match" if match else "q_default"
                chosen = rules[1]["id"] if number == 30 else rules[0]["id"] if match else None
                checks.update(pathMatches=response["path"] == ["q_number", target],
                              matchedPriorityAndDefault=response["trace"][0]["matchedRuleId"] == chosen,
                              normalizedAnswerMatches=response["activeAnswers"] == {"q_number": number},
                              completeWithoutErrors=response["complete"] and not response["errors"])
            event.update(input=number, mode=request["mode"], path=response["path"],
                         matchedRuleId=response["trace"][0]["matchedRuleId"])
        self.state["events"].append(event)
        self.state["allObservedChecksPassed"] = all(all(item["checks"].values()) for item in self.state["events"])
        self.save()

    async def __call__(self, scope, receive, send):
        prefix = self.state.get("managementPath", "")
        if scope["type"] != "http" or (scope.get("method"), scope.get("path")) not in {
                ("POST", prefix + "/preview"), ("PUT", prefix + "/draft")}:
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
        try:
            status = next(message["status"] for message in messages if message["type"] == "http.response.start")
            result = b"".join(message.get("body", b"") for message in messages if message["type"] == "http.response.body")
            await self.observe(scope, json.loads(body), status, json.loads(result))
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
    audit = CombinationAudit(app, output)
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
                    result = await client.post("/api/surveys", headers=headers, json={"title": definition()["title"]})
                    assert result.status_code == 201
                    survey = result.json()
                    path = f'/api/surveys/{survey["id"]}'
                    result = await client.put(path + "/draft", headers=headers,
                        json={"expectedDraftRevision": 0, "definition": definition()})
                    assert result.status_code == 200
                audit.baseline = await snapshot(application.state.db)
                audit.state.update(origin=origin, username=USERNAME, managementPath=path,
                                   baselineCounts={name: len(items) for name, items in audit.baseline.items()},
                                   baselineSha256=fingerprint(audit.baseline), lastSnapshotSha256=fingerprint(audit.baseline))
                audit.save()
                print("COMBINATION_BROWSER_READY " + json.dumps({"origin": origin, "username": USERNAME,
                    "stateFile": str(output / "state.json")}, ensure_ascii=False), flush=True)
                yield
            finally:
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
