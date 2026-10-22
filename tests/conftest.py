from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest_asyncio
from asgi_lifespan import LifespanManager

from app.config import Settings
from app.main import create_app


@pytest_asyncio.fixture
async def context():
    settings = replace(Settings.from_env(), database="survey_test_" + uuid4().hex, testing=True)
    application = create_app(settings)
    async with LifespanManager(application):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://test") as client:
            try:
                yield SimpleNamespace(app=application, client=client, db=application.state.db, settings=settings)
            finally:
                # This fixture owns only the explicitly generated, isolated test database.
                assert settings.database.startswith("survey_test_") and len(settings.database) == 44
                await application.state.db.client.drop_database(settings.database)


async def account(context, username="owner_user"):
    response = await context.client.post("/api/auth/register", json={"username": username, "password": "TestPassword2026!"})
    assert response.status_code == 201, response.text
    body = response.json()
    return {"Authorization": "Bearer " + body["accessToken"]}, body["user"]


async def published(context, headers, definition, multiple=False):
    response = await context.client.post("/api/surveys", headers=headers, json={"title": definition["title"]})
    assert response.status_code == 201, response.text
    survey = response.json()
    if multiple:
        response = await context.client.patch(f'/api/surveys/{survey["id"]}/settings', headers=headers,
                                             json={"expectedSettingsRevision": 0, "allowMultiple": True})
        assert response.status_code == 200, response.text
        survey = response.json()
    response = await context.client.put(f'/api/surveys/{survey["id"]}/draft', headers=headers,
                                       json={"expectedDraftRevision": 0, "definition": definition})
    assert response.status_code == 200, response.text
    survey = response.json()
    response = await context.client.post(f'/api/surveys/{survey["id"]}/publish', headers=headers,
                                        json={"expectedDraftRevision": survey["draftRevision"],
                                              "expectedCurrentVersionId": None, "publishKey": str(uuid4())})
    assert response.status_code == 201, response.text
    version = response.json()
    response = await context.client.get(f'/api/surveys/{survey["id"]}', headers=headers)
    return response.json(), version


async def start(context, headers, survey):
    response = await context.client.post(f'/api/survey/{survey["publicId"]}/start', headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


async def submit(context, headers, survey, attempt, answers, key=None, anonymous=True):
    actual_headers = {**headers, "Idempotency-Key": key or str(uuid4())}
    if attempt.get("fillToken"):
        actual_headers["X-Fill-Token"] = attempt["fillToken"]
    return await context.client.post(f'/api/survey/{survey["publicId"]}/submit', headers=actual_headers,
                                     json={"versionId": attempt["versionId"], "anonymous": anonymous, "answers": answers})
