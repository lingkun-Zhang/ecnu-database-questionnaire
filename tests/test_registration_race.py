"""T01: normalized username collisions at real concurrent insert boundaries.

The test-only wrapper pauses entry into insert_one, then delegates unchanged to
the original PyMongo method. Password hashing, HTTP, writes and unique-index
errors are real. No winner order, wall-clock overlap or load capacity is assumed.
"""
import asyncio
from datetime import datetime
import json

import pytest

from conftest import account, published, start, submit
from scripts.fixtures import example_definition


COLLISIONS = [
    pytest.param("ascii-compatibility", "race_user", [
        ("Race_User", "Race_User"), ("race_user", "race_user"),
        ("RACE_USER", "RACE_USER"), ("  Race_User  ", "Race_User"),
        ("Ｒａｃｅ＿Ｕｓｅｒ", "Race_User"), ("　ＲＡＣＥ＿ＵＳＥＲ　", "RACE_USER"),
        ("\trace_user\n", "race_user"), ("\u2003Race_User\u00a0", "Race_User"),
    ], id="ascii-width-whitespace"),
    pytest.param("casefold-expansion", "strasse_1", [
        ("Straße_1", "Straße_1"), ("STRASSE_1", "STRASSE_1"),
        ("strasse_1", "strasse_1"), ("straße_1", "straße_1"),
        ("ＳＴＲＡＳＳＥ＿１", "STRASSE_1"), ("  Straße_1  ", "Straße_1"),
        ("Ｓｔｒａßｅ＿１", "Straße_1"), ("\t STRASSE_1 \n", "STRASSE_1"),
    ], id="unicode-casefold-expansion"),
]


async def snapshot(context):
    return {name: await context.db[name].find({}).sort("_id", 1).to_list(length=None)
            for name in ("users", "surveys", "survey_versions", "responses")}


async def same_documents(context, expected, label):
    # Keep password hashes and response identities out of assertion output.
    equal = await snapshot(context) == expected
    assert equal, f"{label}: persisted business documents changed"


async def seed_existing_business(context):
    headers, _ = await account(context, "existing_registration_owner")
    survey, _ = await published(context, headers, example_definition())
    attempt = await start(context, headers, survey)
    response = await submit(context, headers, survey, attempt,
                            {"q_age": 17, "q_hobby": ["read"], "q_score": 0}, anonymous=False)
    assert response.status_code == 201
    before = await snapshot(context)
    assert all(len(documents) == 1 for documents in before.values())
    return before


def proof(name, **details):
    # Synthetic labels and observed counts/statuses only: no token, password,
    # stored hash, random database name or internal identifier in evidence.
    print("REGISTRATION_RACE_RESULT " + json.dumps({"scenario": name, **details}, ensure_ascii=False))


async def race(context, monkeypatch, candidates, before):
    """Wait until every real request has normalized and hashed before any insert."""
    collection_type = type(context.db.users)
    original_insert = collection_type.insert_one
    entered, release = asyncio.Event(), asyncio.Event()
    prepared = []
    tasks = []

    async def pause_then_insert(collection, document, *args, **kwargs):
        if collection.full_name == context.db.users.full_name:
            prepared.append({"id": document["_id"], "display": document["username"],
                             "key": document["usernameKey"]})
            if len(prepared) == len(candidates):
                entered.set()
            await release.wait()
        return await original_insert(collection, document, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(collection_type, "insert_one", pause_then_insert)
        try:
            tasks = [asyncio.create_task(context.client.post("/api/auth/register",
                json={"username": name, "password": password})) for name, password in candidates]
            await asyncio.wait_for(entered.wait(), 10)
            assert len(prepared) == len(candidates)
            assert len({item["id"] for item in prepared}) == len(candidates)
            assert all(not task.done() for task in tasks)
            await same_documents(context, before, "all-ready-before-real-inserts")
            release.set()
            outcomes = await asyncio.wait_for(asyncio.gather(*tasks), 10)
        finally:
            release.set()
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
    assert collection_type.insert_one is original_insert
    return outcomes, prepared


def public_login_body(response, status):
    assert response.status_code == status
    body = response.json()
    assert set(body) == {"accessToken", "expiresAt", "tokenType", "user"}
    valid_token = body["tokenType"] == "bearer" and isinstance(body["accessToken"], str) and bool(body["accessToken"])
    assert valid_token, "Authentication result lacks a bearer token"
    assert set(body["user"]) == {"id", "username", "createdAt"}
    return body


def auth_error(response, status, code):
    assert response.status_code == status
    body = response.json()
    assert body["code"] == code
    assert set(body) == {"code", "message", "fieldErrors", "requestId"}


async def verify_token(context, login_body):
    me = await context.client.get("/api/auth/me",
        headers={"Authorization": "Bearer " + login_body["accessToken"]})
    assert me.status_code == 200
    same_user = me.json() == login_body["user"]
    assert same_user, "Issued access token belongs to a different public user"


def unrelated_unchanged(after, before):
    equal = all(after[name] == before[name] for name in ("surveys", "survey_versions", "responses"))
    assert equal, "Concurrent registration changed existing survey/version/response documents"
    existing = before["users"][0]
    same_owner = next(item for item in after["users"] if item["_id"] == existing["_id"]) == existing
    assert same_owner, "Concurrent registration changed the existing owner's document"


@pytest.mark.parametrize("scenario,expected_key,names", COLLISIONS)
async def test_normalized_username_race_creates_only_the_winners_account(
        context, monkeypatch, scenario, expected_key, names):
    before = await seed_existing_business(context)
    info = await context.db.users.index_information()
    assert info["uq_users_username_key"]["unique"] is True
    assert info["uq_users_username_key"]["key"] == [("usernameKey", 1)]
    candidates = [(name, f"RegistrationRace2026!_{index}") for index, (name, _) in enumerate(names)]
    outcomes, prepared = await race(context, monkeypatch, candidates, before)
    assert {item["key"] for item in prepared} == {expected_key}
    assert len({item["id"] for item in prepared}) == 8  # Not an _id collision.
    winners = [index for index, response in enumerate(outcomes) if response.status_code == 201]
    assert len(winners) == 1
    winner = winners[0]
    for index, response in enumerate(outcomes):
        if index != winner:
            auth_error(response, 409, "USERNAME_EXISTS")
    created = public_login_body(outcomes[winner], 201)
    assert created["user"]["username"] == names[winner][1]
    await verify_token(context, created)

    after = await snapshot(context)
    unrelated_unchanged(after, before)
    assert len(after["users"]) == 2
    matching = [item for item in after["users"] if item["usernameKey"] == expected_key]
    assert len(matching) == 1
    stored = matching[0]
    assert stored["_id"] == created["user"]["id"]
    assert stored["username"] == names[winner][1] and stored["schemaVersion"] == 1
    assert stored["createdAt"] == datetime.fromisoformat(created["user"]["createdAt"])
    argon2_hash = stored["passwordHash"].startswith("$argon2id$")
    assert argon2_hash, "Stored winner password is not an Argon2id hash"
    assert set(stored) == {"_id", "username", "usernameKey", "passwordHash", "createdAt", "schemaVersion"}

    # Every alias must authenticate the same winner, not a partly created loser.
    winner_password = candidates[winner][1]
    alias_statuses = []
    for name, _ in candidates:
        login = await context.client.post("/api/auth/login", json={"username": name, "password": winner_password})
        body = public_login_body(login, 200)
        same_user = body["user"] == created["user"]
        assert same_user, "A normalized alias authenticated a different user"
        await verify_token(context, body)
        alias_statuses.append(login.status_code)

    loser_statuses = []
    for index, (_, password) in enumerate(candidates):
        if index != winner:
            denied = await context.client.post("/api/auth/login",
                json={"username": expected_key, "password": password})
            auth_error(denied, 401, "INVALID_CREDENTIALS")
            loser_statuses.append(denied.status_code)
    retry = await context.client.post("/api/auth/register",
        json={"username": names[(winner + 1) % len(names)][0], "password": "DoNotOverwrite2026!"})
    auth_error(retry, 409, "USERNAME_EXISTS")
    await same_documents(context, after, "alias-logins-failed-passwords-and-duplicate-retry")
    proof(scenario, requests=8, readyBeforeInserts=len(prepared), uniquePreparedIds=8,
          uniquePreparedUsernameKeys=len({item["key"] for item in prepared}),
          allCollectionsUnchangedBeforeRelease=True, requestStatuses=[item.status_code for item in outcomes],
          winnerIndex=winner, created=1, conflicts=7, newUserCount=len(after["users"]) - len(before["users"]),
          uniqueIndexVerified=True, winnerTokenValid=True, displayAndCreatedAtMatch=True,
          winnerPasswordAliasLoginStatuses=alias_statuses, loserPasswordLoginStatuses=loser_statuses,
          duplicateRetryStatus=retry.status_code, wholeDatabaseUnchangedAfterAuthentication=True,
          existingBusinessUnchanged=True, insertWrapperRestored=True)


async def test_distinct_normalized_usernames_can_register_concurrently(context, monkeypatch):
    before = await seed_existing_business(context)
    names = [("Race_User", "Race_User", "race_user"),
             ("RaceUser", "RaceUser", "raceuser"),
             ("Race_User_2", "Race_User_2", "race_user_2"),
             ("Ｒａｃｅ＿Ｕｓｅｒ＿３", "Race_User_3", "race_user_3")]
    candidates = [(name, f"DistinctRegistration2026!_{index}") for index, (name, _, _) in enumerate(names)]
    outcomes, prepared = await race(context, monkeypatch, candidates, before)
    assert {item["key"] for item in prepared} == {key for _, _, key in names}
    assert len({item["key"] for item in prepared}) == 4
    created = [public_login_body(response, 201) for response in outcomes]
    assert len({body["user"]["id"] for body in created}) == 4
    after = await snapshot(context)
    unrelated_unchanged(after, before)
    assert len(after["users"]) == len(before["users"]) + 4
    for index, ((_, display, key), body) in enumerate(zip(names, created)):
        assert body["user"]["username"] == display
        stored = next(item for item in after["users"] if item["usernameKey"] == key)
        assert stored["_id"] == body["user"]["id"] and stored["username"] == display
        await verify_token(context, body)
        login = await context.client.post("/api/auth/login",
            json={"username": key, "password": candidates[index][1]})
        actual = public_login_body(login, 200)
        same_user = actual["user"] == body["user"]
        assert same_user, "Distinct normalized usernames were merged"
        denied = await context.client.post("/api/auth/login",
            json={"username": key, "password": candidates[(index + 1) % 4][1]})
        auth_error(denied, 401, "INVALID_CREDENTIALS")
    await same_documents(context, after, "distinct-accounts-authentication")
    proof("distinct-normalized-keys", requests=4, readyBeforeInserts=len(prepared),
          uniquePreparedIds=len({item["id"] for item in prepared}), uniquePreparedUsernameKeys=4,
          allCollectionsUnchangedBeforeRelease=True, requestStatuses=[item.status_code for item in outcomes],
          created=4, newUserCount=len(after["users"]) - len(before["users"]),
          ownPasswordsAccepted=4, otherAccountPasswordsRejected=4, issuedTokensValid=True,
          wholeDatabaseUnchangedAfterAuthentication=True, existingBusinessUnchanged=True,
          insertWrapperRestored=True)
