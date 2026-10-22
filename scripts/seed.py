"""Populate the documented six-response example through the real HTTP API."""
import argparse
import asyncio
from uuid import uuid4, uuid5, NAMESPACE_URL

import httpx

from fixtures import example_definition, ROWS

PASSWORD = "DemoLab2026!"


async def seed(base_url):
    async with httpx.AsyncClient(base_url=base_url.rstrip("/") + "/api", timeout=30, trust_env=False) as client:
        async def call(method, path, headers=None, **kwargs):
            response = await client.request(method, path, headers=headers, **kwargs)
            if response.status_code >= 400:
                body = response.json()
                raise RuntimeError(f'{method} {path}: {body.get("code")} {body.get("message")}')
            return response.json()

        async def login_or_register(username):
            response = await client.post("/auth/register", json={"username": username, "password": PASSWORD})
            if response.status_code == 409:
                body = await call("POST", "/auth/login", json={"username": username, "password": PASSWORD})
            else:
                response.raise_for_status()
                body = response.json()
            return {"Authorization": "Bearer " + body["accessToken"]}

        owner = await login_or_register("demo_owner")
        definition = example_definition()
        items = (await call("GET", "/surveys?limit=100", owner))["items"]
        survey = next((s for s in items if s["draft"]["title"] == definition["title"]), None)
        if survey is None:
            survey = await call("POST", "/surveys", owner, json={"title": definition["title"]})
        sid = survey["id"]
        if not survey["currentVersionId"]:
            survey = await call("PATCH", f"/surveys/{sid}/settings", owner,
                                json={"expectedSettingsRevision": survey["settingsRevision"], "allowMultiple": True})
            survey = await call("PUT", f"/surveys/{sid}/draft", owner,
                                json={"expectedDraftRevision": survey["draftRevision"], "definition": definition})
            version = await call("POST", f"/surveys/{sid}/publish", owner,
                                 json={"expectedDraftRevision": survey["draftRevision"],
                                       "expectedCurrentVersionId": None, "publishKey": str(uuid4())})
        else:
            version = await call("GET", f'/surveys/{sid}/versions/{survey["currentVersionId"]}', owner)
            if version["definition"] != definition:
                raise RuntimeError("已有示例问卷的内容已修改；保留现有数据，请另建示例账号或问卷。")
        accounts = {}
        for index, (username, anonymous, answers) in enumerate(ROWS, 1):
            if username not in accounts:
                accounts[username] = await login_or_register(username)
            headers = accounts[username]
            attempt = await call("POST", f'/survey/{survey["publicId"]}/start', headers)
            headers = {**headers, "X-Fill-Token": attempt["fillToken"],
                       "Idempotency-Key": str(uuid5(NAMESPACE_URL, f"{sid}:fixture:R{index}"))}
            await call("POST", f'/survey/{survey["publicId"]}/submit', headers,
                       json={"versionId": version["id"], "anonymous": anonymous, "answers": answers})
        stats = await call("GET", f'/surveys/{sid}/stats?versionId={version["id"]}', owner)
        print(f'示例已就绪：{stats["totalResponses"]}份答卷，{stats["uniqueParticipants"]}位参与者。')
        print(f"演示账号：demo_owner / {PASSWORD}（仅供本地教学示例）")
        print(f'填写链接：{base_url.rstrip("/")}/survey/{survey["publicId"]}')


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    asyncio.run(seed(parser.parse_args().base_url))
