"""Disposable browser fixture: replace one committed 201 with a synthetic 503.

This is an application HTTP-response fault, not TCP packet loss. It creates a
random, isolated MongoDB database and independent secrets. A stop.request file
in the output directory requests graceful shutdown and database cleanup.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
import hashlib
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

USERNAME = "recovery_browser_test"
PASSWORD = "RecoveryBrowser2026!"  # Disposable fixture credentials, never a real account.


class ReceiptLoss:
    def __init__(self, app, output, state):
        self.app, self.output, self.state = app, output, state

    def save(self):
        temporary = self.output / "state.tmp"
        temporary.write_text(json.dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.output / "state.json")

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        if scope["type"] != "http" or scope.get("method") != "POST" or not path.startswith("/api/survey/"):
            await self.app(scope, receive, send)
            return
        messages, request_body = [], bytearray()

        async def capture_receive():
            message = await receive()
            if message["type"] == "http.request":
                request_body.extend(message.get("body", b""))
            return message

        async def capture_send(message):
            messages.append(message)

        await self.app(scope, capture_receive, capture_send)
        status = next(m["status"] for m in messages if m["type"] == "http.response.start")
        event = {"operation": path.rsplit("/", 1)[-1], "serverStatus": status, "clientStatus": status}
        if event["operation"] == "submit":
            headers = {key.decode().lower(): value.decode() for key, value in scope["headers"]}
            event["key"] = headers.get("idempotency-key")
            event["request"] = json.loads(request_body)
            # Compare credentials without recording a usable token.
            event["fillTokenDigest"] = hashlib.sha256(headers.get("x-fill-token", "").encode()).hexdigest()
            body = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
            value = json.loads(body)
            if status == 201 and self.state["originalReceipt"] is None:
                self.state["originalReceipt"] = value
                event["clientStatus"] = 503
                event["fault"] = "replace_committed_201_with_503"
            elif status == 200:
                event["sameReceipt"] = value == self.state["originalReceipt"]
            survey = await self.app.state.db.surveys.find_one({"_id": self.state["surveyId"]})
            event["storedCount"] = await self.app.state.db.responses.count_documents({"surveyId": survey["_id"]})
            event["submitSequence"] = survey["submitSequence"]
            event["receipt"] = value if value.get("receiptId") else None
        self.state["events"].append(event)
        self.save()
        if event["clientStatus"] == 503 and event.get("fault"):
            body = json.dumps({"code": "SIMULATED_RECEIPT_LOSS", "message": "测试响应中断：请保留原提交标识并确认结果。"}, ensure_ascii=False).encode()
            await send({"type": "http.response.start", "status": 503,
                        "headers": [(b"content-type", b"application/json; charset=utf-8"),
                                    (b"content-length", str(len(body)).encode()), (b"cache-control", b"no-store")]})
            await send({"type": "http.response.body", "body": body})
        else:
            for message in messages:
                await send(message)


async def run(output):
    output.mkdir(parents=True, exist_ok=False)
    database = "survey_test_" + uuid4().hex
    settings = replace(Settings.from_env(), database=database, testing=True,
                       auth_secret=secrets.token_hex(32), fill_secret=secrets.token_hex(32),
                       participant_secret=secrets.token_hex(32))
    app = create_app(settings)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    state = {"database": database, "origin": f"http://127.0.0.1:{port}", "username": USERNAME,
             "faultModel": "application_http_response_replacement", "originalReceipt": None,
             "events": [], "cleanedUp": False}
    wrapped = ReceiptLoss(app, output, state)
    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application):
        async with original_lifespan(application):
            try:
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://fixture") as client:
                    result = await client.post("/api/auth/register", json={"username": USERNAME, "password": PASSWORD})
                    assert result.status_code == 201
                    headers = {"Authorization": "Bearer " + result.json()["accessToken"]}
                    created = await client.post("/api/surveys", headers=headers, json={"title": "回执恢复验收（独立测试）"})
                    assert created.status_code == 201
                    survey = created.json()
                    definition = {"title": "回执恢复验收（独立测试）", "description": "模拟保存成功后的响应确认中断。",
                                  "allowAnonymous": True, "entryQuestionId": "q_recovery", "questions": [
                                      {"id": "q_recovery", "kind": "number", "title": "请输入测试数字42", "required": True,
                                       "config": {"min": 0, "max": 100, "integerOnly": True}, "defaultTarget": "END", "rules": []}]}
                    saved = await client.put(f'/api/surveys/{survey["id"]}/draft', headers=headers,
                                             json={"expectedDraftRevision": 0, "definition": definition})
                    assert saved.status_code == 200
                    published = await client.post(f'/api/surveys/{survey["id"]}/publish', headers=headers,
                                                  json={"expectedDraftRevision": 1, "expectedCurrentVersionId": None,
                                                        "publishKey": str(uuid4())})
                    assert published.status_code == 201
                state.update(surveyId=survey["id"], publicId=survey["publicId"], versionId=published.json()["id"],
                             fillURL=f'{state["origin"]}/survey/{survey["publicId"]}')
                wrapped.save()
                print("RECOVERY_BROWSER_READY " + json.dumps({"fillURL": state["fillURL"], "stateFile": str(output / "state.json"),
                                                              "username": USERNAME}, ensure_ascii=False), flush=True)
                yield
            finally:
                # This runner only owns this exact randomly generated database.
                assert settings.database == database and re.fullmatch(r"survey_test_[0-9a-f]{32}", database)
                await application.state.db.client.drop_database(database)
                state["cleanedUp"] = True
                wrapped.save()

    app.router.lifespan_context = lifespan
    server = uvicorn.Server(uvicorn.Config(wrapped, log_level="warning", access_log=False))

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
        listener.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path, help="A new directory for fixture evidence and stop.request")
    args = parser.parse_args()
    asyncio.run(run(args.output.resolve()))
