"""Real API/MongoDB checks for one respondent cohort across every owner view."""

import csv
import io
from uuid import uuid4

import pytest

from conftest import account, published, start, submit
from scripts.fixtures import example_definition


MODES = ("all", "anonymous", "named")
PRIVATE_KEYS = {"participantKey", "identity", "oneTimeParticipantKey", "payloadDigest",
                "userId", "usernameAtSubmit", "submissionKey"}


def keys_in(value):
    if isinstance(value, dict):
        return set(value).union(*(keys_in(item) for item in value.values()))
    if isinstance(value, list):
        return set().union(*(keys_in(item) for item in value))
    return set()


def answers(score=None, note=None):
    result = {"q_age": 17, "q_hobby": ["read"]}
    if score is not None:
        result["q_score"] = score
    if note is not None:
        result["q_note"] = note
    return result


async def accepted(context, headers, survey, attempt, score=None, anonymous=True, note=None):
    response = await submit(context, headers, survey, attempt, answers(score, note), anonymous=anonymous)
    assert response.status_code == 201, response.text
    return response.json()["receiptId"]


async def owner_get(context, headers, survey, suffix, params):
    response = await context.client.get(f'/api/surveys/{survey["id"]}/{suffix}',
                                        headers=headers, params=params)
    assert response.status_code == 200, response.text
    return response


async def pages(context, headers, survey, params, after_first_page=None):
    """Follow the actual cursor, including filters, with no client-side filtering."""
    result, nonempty_pages, previous_cursor = [], 0, None
    while True:
        query = {**params, "limit": 1}
        if previous_cursor is not None:
            query["cursor"] = previous_cursor
        page = (await owner_get(context, headers, survey, "responses", query)).json()
        assert page["asOfSeq"] == params["asOfSeq"]
        assert page["respondentType"] == params["respondentType"]
        assert len(page["items"]) <= 1
        if page["items"]:
            nonempty_pages += 1
            result.extend(page["items"])
        cursor = page["nextCursor"]
        if cursor is None:
            break
        assert page["items"], "A nonterminal page must advance over at least one result"
        assert cursor == page["items"][-1]["acceptedSeq"]
        assert previous_cursor is None or cursor < previous_cursor
        if nonempty_pages == 1 and after_first_page is not None:
            await after_first_page()
            after_first_page = None
        previous_cursor = cursor
        assert nonempty_pages < 100, "Cursor did not finish this small fixture"
    sequences = [item["acceptedSeq"] for item in result]
    assert sequences == sorted(set(sequences), reverse=True)
    assert len(result) == len({item["receiptId"] for item in result})
    return result, nonempty_pages


def csv_rows(response, definition):
    reader = csv.DictReader(io.StringIO(response.text.lstrip("\ufeff")))
    assert reader.fieldnames == ["回执", "版本", "提交时间UTC", "匿名", "填写者"] + [
        question["title"] for question in definition["questions"]
    ]
    rows = list(reader)
    assert len(rows) == len({row["回执"] for row in rows})
    assert not PRIVATE_KEYS.intersection(reader.fieldnames)
    return rows


async def test_respondent_cohorts_share_frozen_statistics_cursor_and_csv(context):
    owner, owner_user = await account(context, "cohort_owner")
    participant, participant_user = await account(context, "cohort_participant")
    definition = example_definition()
    survey, version = await published(context, owner, definition, multiple=True)
    owner_attempt = await start(context, owner, survey)
    participant_attempt = await start(context, participant, survey)

    # Both people occur repeatedly in BOTH modes. Adding per-mode participant
    # counts would incorrectly report four people instead of two overall.
    fixtures = [
        (owner, owner_user, owner_attempt, True, 0, "匿名零分"),
        (participant, participant_user, participant_attempt, False, 10, "实名十分"),
        (owner, owner_user, owner_attempt, True, 2, None),
        (participant, participant_user, participant_attempt, False, None, None),
        (participant, participant_user, participant_attempt, True, 4, "匿名四分"),
        (owner, owner_user, owner_attempt, False, 6, "实名六分"),
        (participant, participant_user, participant_attempt, True, None, None),
        (owner, owner_user, owner_attempt, False, 8, None),
    ]
    expected = {"anonymous": set(), "named": set()}
    identities, scored = {}, set()
    for headers, user, attempt, anonymous, score, note in fixtures:
        receipt_id = await accepted(context, headers, survey, attempt, score, anonymous, note)
        expected["anonymous" if anonymous else "named"].add(receipt_id)
        identities[receipt_id] = user["username"]
        if score is not None:
            scored.add(receipt_id)
    expected["all"] = expected["anonymous"] | expected["named"]
    assert not expected["anonymous"].intersection(expected["named"])

    default_stats = (await owner_get(context, owner, survey, "stats", {
        "versionId": version["id"],
    })).json()
    assert default_stats["respondentType"] == "all"
    boundary = default_stats["asOfSeq"]
    assert boundary == 8
    late_receipt = await accepted(context, participant, survey, participant_attempt, 1, False, "冻结后新增")
    interleaved_receipts = []

    async def append_between_pages():
        # Deterministic interleaving: page 1 has returned, page 2 has not been
        # requested. This is an actual API write, not concurrent-client load.
        assert not interleaved_receipts
        interleaved_receipts.append(await accepted(
            context, owner, survey, owner_attempt, 9, True, "第一页和第二页之间新增",
        ))

    receipt_sets = {}
    for mode in MODES:
        params = {"versionId": version["id"], "asOfSeq": boundary, "respondentType": mode}
        stats = (await owner_get(context, owner, survey, "stats", params)).json()
        count = 8 if mode == "all" else 4
        assert (stats["respondentType"], stats["asOfSeq"]) == (mode, boundary)
        assert (stats["totalResponses"], stats["uniqueParticipants"]) == (count, 2)
        assert (stats["anonymousResponses"], stats["namedResponses"]) == {
            "all": (4, 4), "anonymous": (4, 0), "named": (0, 4),
        }[mode]
        assert not keys_in(stats).intersection(PRIVATE_KEYS | {"respondentName"})
        if mode == "all":
            assert stats == default_stats
        questions = {question["questionId"]: question for question in stats["questions"]}
        score = questions["q_score"]
        score_sum, score_avg = {"all": (30, 5), "anonymous": (6, 2), "named": (24, 8)}[mode]
        answered = 6 if mode == "all" else 3
        assert (score["R"], score["V"], score["A"], score["U"], score["S"]) == (
            count, count, answered, count - answered, 0,
        )
        assert score["sum"] == score_sum and score["avg"] == pytest.approx(score_avg)
        assert score["answeredParticipants"] == 2
        assert questions["q_minor"]["U"] == count
        assert questions["q_adult"]["S"] == count
        read = next(option for option in questions["q_hobby"]["options"] if option["id"] == "read")
        assert (read["count"], read["uniqueParticipants"]) == (count, 2)

        detail, nonempty_pages = await pages(
            context, owner, survey, params,
            after_first_page=append_between_pages if mode == "all" else None,
        )
        assert nonempty_pages >= 3
        assert len(interleaved_receipts) == 1
        later_receipts = {late_receipt, *interleaved_receipts}
        detail_ids = {item["receiptId"] for item in detail}
        assert detail_ids == expected[mode] and detail_ids.isdisjoint(later_receipts)
        assert len(detail) == stats["totalResponses"]
        for item in detail:
            assert not keys_in(item).intersection(PRIVATE_KEYS)
            assert item["versionId"] == version["id"] and item["acceptedSeq"] <= boundary
            if mode != "all":
                assert item["anonymous"] is (mode == "anonymous")
            if item["anonymous"]:
                assert "respondentName" not in item
            else:
                assert item["respondentName"] == identities[item["receiptId"]]

        exported = csv_rows(await owner_get(context, owner, survey, "export", params), definition)
        csv_ids = {row["回执"] for row in exported}
        assert csv_ids == detail_ids and len(exported) == stats["totalResponses"]
        assert csv_ids.isdisjoint(later_receipts)
        for row in exported:
            anonymous = row["回执"] in expected["anonymous"]
            assert row["匿名"] == ("是" if anonymous else "否")
            assert row["填写者"] == ("匿名" if anonymous else identities[row["回执"]])
        receipt_sets[mode] = csv_ids
        if mode == "all":
            implicit = {"versionId": version["id"], "asOfSeq": boundary}
            default_detail = (await owner_get(context, owner, survey, "responses", implicit)).json()
            assert default_detail["respondentType"] == "all"
            assert default_detail["asOfSeq"] == boundary and default_detail["nextCursor"] is None
            assert {item["receiptId"] for item in default_detail["items"]} == detail_ids
            default_export = csv_rows(await owner_get(context, owner, survey, "export", implicit), definition)
            assert default_export == exported

        # Question selection narrows the summaries and answered-response detail;
        # it must not switch the respondent cohort or mistake number 0 for blank.
        focused = {**params, "questionId": "q_score"}
        focused_stats = (await owner_get(context, owner, survey, "stats", focused)).json()
        assert focused_stats["respondentType"] == mode
        assert focused_stats["totalResponses"] == count
        assert focused_stats["questions"] == [score]
        focused_items, _ = await pages(context, owner, survey, focused)
        assert {item["receiptId"] for item in focused_items} == expected[mode].intersection(scored)
        assert len(focused_items) == score["A"]
        if mode in ("all", "anonymous"):
            assert any(answer["value"] == 0 for item in focused_items for answer in item["answers"]
                       if answer["questionId"] == "q_score")

    assert receipt_sets["anonymous"].isdisjoint(receipt_sets["named"])
    assert receipt_sets["anonymous"] | receipt_sets["named"] == receipt_sets["all"]
    fresh = (await owner_get(context, owner, survey, "stats", {
        "versionId": version["id"], "respondentType": "named",
    })).json()
    assert fresh["totalResponses"] == 5 and fresh["asOfSeq"] == boundary + 2

    documents = await context.db.responses.find({"surveyId": survey["id"]}).to_list(length=20)
    assert len(documents) == 10 and len({document["participantKey"] for document in documents}) == 2
    for document in documents:
        if document["anonymous"]:
            assert "identity" not in document
        else:
            assert document["identity"]["usernameAtSubmit"] == identities.get(
                document["_id"], participant_user["username"],
            )


async def test_respondent_modes_remain_scoped_to_owner_survey_version_and_empty_cohort(context):
    owner, _ = await account(context, "scope_owner")
    outsider, _ = await account(context, "scope_outsider")
    definition = example_definition()
    survey, first_version = await published(context, owner, definition, multiple=True)
    first_attempt = await start(context, owner, survey)
    first_anonymous = await accepted(context, owner, survey, first_attempt, 0)
    first_named = await accepted(context, owner, survey, first_attempt, 2, False)

    changed = example_definition()
    changed["title"] = "第二个发布版本"
    endpoint = f'/api/surveys/{survey["id"]}'
    draft = await context.client.put(endpoint + "/draft", headers=owner, json={
        "expectedDraftRevision": survey["draftRevision"], "definition": changed,
    })
    assert draft.status_code == 200, draft.text
    published_response = await context.client.post(endpoint + "/publish", headers=owner, json={
        "expectedDraftRevision": draft.json()["draftRevision"],
        "expectedCurrentVersionId": first_version["id"], "publishKey": str(uuid4()),
    })
    assert published_response.status_code == 201, published_response.text
    second_version = published_response.json()
    second_attempt = await start(context, owner, survey)
    second_named = await accepted(context, owner, survey, second_attempt, 8, False)

    other_survey, other_version = await published(context, outsider, definition, multiple=True)
    other_attempt = await start(context, outsider, other_survey)
    other_receipt = await accepted(context, outsider, other_survey, other_attempt, 10)
    expected_by_version = {
        first_version["id"]: {"all": {first_anonymous, first_named},
                              "anonymous": {first_anonymous}, "named": {first_named}},
        second_version["id"]: {"all": {second_named}, "anonymous": set(), "named": {second_named}},
    }
    for version in (first_version, second_version):
        for mode in MODES:
            params = {"versionId": version["id"], "respondentType": mode, "asOfSeq": 3}
            stats = (await owner_get(context, owner, survey, "stats", params)).json()
            expected_ids = expected_by_version[version["id"]][mode]
            detail, _ = await pages(context, owner, survey, params)
            rows = csv_rows(await owner_get(context, owner, survey, "export", params), version["definition"])
            assert stats["respondentType"] == mode and stats["totalResponses"] == len(expected_ids)
            assert stats["uniqueParticipants"] == int(bool(expected_ids))
            assert {item["receiptId"] for item in detail} == {row["回执"] for row in rows} == expected_ids
            assert other_receipt not in expected_ids
            if not expected_ids:
                assert stats["anonymousResponses"] == stats["namedResponses"] == 0
                score = next(question for question in stats["questions"] if question["questionId"] == "q_score")
                assert (score["R"], score["V"], score["A"], score["U"], score["S"]) == (0, 0, 0, 0, 0)
                assert score["sum"] == 0 and score["avg"] is None

    # A historical upper bound must still exclude later named submissions.
    for suffix in ("stats", "responses", "export"):
        params = {"versionId": first_version["id"], "respondentType": "named", "asOfSeq": 1}
        response = await owner_get(context, owner, survey, suffix, params)
        if suffix == "stats":
            assert response.json()["totalResponses"] == 0
        elif suffix == "responses":
            assert response.json()["items"] == [] and response.json()["nextCursor"] is None
        else:
            assert csv_rows(response, definition) == []

    for mode in MODES:
        for suffix in ("stats", "responses", "export"):
            # A valid version of a different survey cannot be used as a join key.
            wrong_version = await context.client.get(endpoint + "/" + suffix, headers=owner, params={
                "versionId": other_version["id"], "respondentType": mode,
            })
            assert wrong_version.status_code == 404, wrong_version.text
            rejected = await context.client.get(endpoint + "/" + suffix, headers=outsider, params={
                "versionId": first_version["id"], "respondentType": mode,
            })
            assert rejected.status_code == 404, rejected.text
            anonymous = await context.client.get(endpoint + "/" + suffix, params={
                "versionId": first_version["id"], "respondentType": mode,
            })
            assert anonymous.status_code == 401, anonymous.text


async def test_invalid_respondent_mode_is_rejected_by_all_owner_endpoints(context):
    owner, _ = await account(context, "invalid_cohort_owner")
    survey, version = await published(context, owner, example_definition())
    for suffix in ("stats", "responses", "export"):
        for invalid_mode in ("bogus", "ANONYMOUS", "true", ""):
            response = await context.client.get(f'/api/surveys/{survey["id"]}/{suffix}',
                headers=owner, params={"versionId": version["id"], "respondentType": invalid_mode})
            assert response.status_code == 422, (suffix, invalid_mode, response.text)
    assert await context.db.responses.count_documents({}) == 0
