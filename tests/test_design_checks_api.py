"""Real API/Mongo boundaries for advisory-only questionnaire design checks."""

from copy import deepcopy
from uuid import uuid4

from conftest import account, start, submit


def advisory_definition():
    def note(qid):
        return {"id": qid, "kind": "text", "title": qid, "required": False,
                "config": {"minLength": 1, "maxLength": 100},
                "defaultTarget": "END", "rules": []}

    return {
        "title": "设计提示不会改变正式路由", "description": "隔离数据库中的 API 集成测试。",
        "allowAnonymous": True, "entryQuestionId": "q_gate", "questions": [
            {"id": "q_gate", "kind": "number", "title": "分流数字", "required": True,
             "config": {"min": 0, "max": 100, "integerOnly": True}, "defaultTarget": "END",
             "rules": [
                 {"id": "r_first", "priority": 10, "condition": {"operator": "lt", "value": 50},
                  "target": "q_first"},
                 {"id": "r_duplicate", "priority": 20, "condition": {"operator": "lt", "value": 50},
                  "target": "q_duplicate"},
                 {"id": "r_shadow", "priority": 30, "condition": {"operator": "lt", "value": 40},
                  "target": "q_shadow"},
                 {"id": "r_overlap", "priority": 40, "condition": {"operator": "gte", "value": 40},
                  "target": "q_last"},
             ]},
            note("q_first"), note("q_duplicate"), note("q_shadow"), note("q_last"), note("q_orphan"),
        ],
    }


async def create_draft(context, headers, definition):
    created = await context.client.post("/api/surveys", headers=headers, json={"title": definition["title"]})
    assert created.status_code == 201, created.text
    endpoint = f'/api/surveys/{created.json()["id"]}'
    saved = await context.client.put(endpoint + "/draft", headers=headers,
                                     json={"expectedDraftRevision": 0, "definition": definition})
    assert saved.status_code == 200, saved.text
    return endpoint, saved.json()


async def test_preview_returns_locatable_advisories_without_writing_draft_or_answers(context):
    owner, _ = await account(context, "design_preview_owner")
    definition = advisory_definition()
    endpoint, survey = await create_draft(context, owner, definition)
    before = await context.db.surveys.find_one({"_id": survey["id"]})
    warning_sets = []

    for mode in ("interactive", "final"):
        response = await context.client.post(endpoint + "/preview", headers=owner,
            json={"definition": definition, "answers": {"q_gate": 45}, "mode": mode})
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["errors"] == [] and body["complete"] is True
        assert body["path"] == ["q_gate", "q_first"]
        assert body["trace"][0]["matchedRuleId"] == "r_first"
        warnings = body["warnings"]
        warning_sets.append(warnings)
        codes = {warning["code"] for warning in warnings}
        assert {"unreachable_question", "duplicate_rule_condition", "shadowed_rule", "overlapping_rules"} <= codes
        question_ids = {question["id"] for question in definition["questions"]}
        rule_ids = {rule["id"] for question in definition["questions"] for rule in question["rules"]}
        for warning in warnings:
            assert warning["questionId"] in question_ids
            assert warning["ruleId"] is None or warning["ruleId"] in rule_ids
            assert isinstance(warning["message"], str) and warning["message"].strip()
            assert isinstance(warning["field"], str) and warning["field"].startswith("questions[")
            assert isinstance(warning["relatedRuleIds"], list)
            assert set(warning["relatedRuleIds"]) <= rule_ids
        assert any(warning["code"] == "unreachable_question" and warning["questionId"] == "q_orphan"
                   for warning in warnings)
        for code, rule_id, index in (("duplicate_rule_condition", "r_duplicate", 1),
                                     ("shadowed_rule", "r_shadow", 2),
                                     ("overlapping_rules", "r_overlap", 3)):
            matching = [warning for warning in warnings if warning["code"] == code and warning["ruleId"] == rule_id]
            assert matching and any("r_first" in warning["relatedRuleIds"] for warning in matching)
            assert all(warning["field"].startswith(f"questions[0].rules[{index}]") for warning in matching)

    assert warning_sets[0] == warning_sets[1]
    assert await context.db.surveys.find_one({"_id": survey["id"]}) == before
    assert before["draft"] == definition
    assert await context.db.survey_versions.count_documents({"surveyId": survey["id"]}) == 0
    assert await context.db.responses.count_documents({"surveyId": survey["id"]}) == 0


async def test_warning_definition_publishes_idempotently_and_keeps_live_evaluation_unchanged(context):
    owner, _ = await account(context, "design_publish_owner")
    participant, _ = await account(context, "design_publish_participant")
    definition = advisory_definition()
    endpoint, survey = await create_draft(context, owner, definition)
    # Invalid cached values on inactive branches must remain irrelevant to filling.
    answers = {"q_gate": 45, "q_first": "命中第一条规则", "q_duplicate": {"stale": True},
               "q_shadow": 99, "q_last": ["stale"], "q_orphan": "未经过的题目"}
    preview = await context.client.post(endpoint + "/preview", headers=owner,
        json={"definition": definition, "answers": answers, "mode": "final"})
    assert preview.status_code == 200 and preview.json()["warnings"], preview.text

    publication = {"expectedDraftRevision": survey["draftRevision"],
                   "expectedCurrentVersionId": None, "publishKey": str(uuid4())}
    first = await context.client.post(endpoint + "/publish", headers=owner, json=publication)
    replay = await context.client.post(endpoint + "/publish", headers=owner, json=publication)
    assert first.status_code == 201 and replay.status_code == 200, (first.text, replay.text)
    assert first.json() == replay.json()
    version = first.json()
    assert version["definition"] == definition and "warnings" not in version
    assert await context.db.survey_versions.count_documents({"surveyId": survey["id"]}) == 1
    snapshot = await context.db.survey_versions.find_one({"_id": version["id"]})
    assert snapshot["definition"] == definition and "warnings" not in snapshot

    attempt = await start(context, participant, survey)
    assert attempt["definition"] == definition and "warnings" not in attempt
    public = await context.client.get(f'/api/survey/{survey["publicId"]}', headers=participant)
    assert public.status_code == 200 and "warnings" not in public.json(), public.text
    live = await context.client.post(f'/api/survey/{survey["publicId"]}/evaluate',
        headers={**participant, "X-Fill-Token": attempt["fillToken"]},
        json={"versionId": version["id"], "answers": answers})
    assert live.status_code == 200, live.text
    assert live.json() == {key: value for key, value in preview.json().items() if key != "warnings"}
    accepted = await submit(context, participant, survey, attempt, answers)
    assert accepted.status_code == 201, accepted.text
    stored = await context.db.responses.find_one({"surveyId": survey["id"]})
    assert stored["path"] == preview.json()["path"] == ["q_gate", "q_first"]
    assert {answer["questionId"] for answer in stored["answers"]} == {"q_gate", "q_first"}
    assert "warnings" not in stored and "warnings" not in accepted.json()
    assert await context.db.survey_versions.find_one({"_id": version["id"]}) == snapshot
    draft = await context.db.surveys.find_one({"_id": survey["id"]})
    assert draft["draft"] == definition and "warnings" not in draft


async def test_invalid_definition_still_blocks_preview_and_publication_with_field_errors(context):
    owner, _ = await account(context, "design_invalid_owner")
    definition = advisory_definition()
    definition["questions"][0]["config"] = {"min": 100, "max": 0, "integerOnly": True}
    definition["questions"][0]["defaultTarget"] = "q_missing"
    endpoint, survey = await create_draft(context, owner, definition)

    requests = [
        ("/preview", {"definition": definition, "answers": {}, "mode": "final"}),
        ("/publish", {"expectedDraftRevision": survey["draftRevision"],
                      "expectedCurrentVersionId": None, "publishKey": str(uuid4())}),
    ]
    for suffix, body in requests:
        response = await context.client.post(endpoint + suffix, headers=owner, json=body)
        assert response.status_code == 422, response.text
        error = response.json()
        assert error["code"] == "INVALID_DEFINITION" and "warnings" not in error
        assert {"bound_order", "invalid_target"} <= {item["code"] for item in error["fieldErrors"]}
        assert all(item["field"] for item in error["fieldErrors"])

    unchanged = await context.db.surveys.find_one({"_id": survey["id"]})
    assert unchanged["draft"] == definition and unchanged["currentVersionId"] is None
    assert unchanged["status"] == "draft" and unchanged["submitSequence"] == 0
    assert await context.db.survey_versions.count_documents({"surveyId": survey["id"]}) == 0
    assert await context.db.responses.count_documents({"surveyId": survey["id"]}) == 0


async def test_preview_advisories_respect_owner_authorization_before_definition_checks(context):
    owner, _ = await account(context, "design_private_owner")
    other, _ = await account(context, "design_other_user")
    definition = advisory_definition()
    endpoint, survey = await create_draft(context, owner, definition)
    before = await context.db.surveys.find_one({"_id": survey["id"]})
    malformed = deepcopy(definition)
    malformed["questions"][0]["defaultTarget"] = "q_missing"

    for supplied in (definition, malformed):
        response = await context.client.post(endpoint + "/preview", headers=other,
            json={"definition": supplied, "answers": {}, "mode": "final"})
        assert response.status_code == 404, response.text
        error = response.json()
        assert error["code"] == "NOT_FOUND" and error["fieldErrors"] == []
        assert "warnings" not in error and "unreachable_question" not in response.text
        assert definition["title"] not in response.text and "q_orphan" not in response.text
    unauthenticated = await context.client.post(endpoint + "/preview",
        json={"definition": definition, "answers": {}, "mode": "final"})
    assert unauthenticated.status_code == 401 and "warnings" not in unauthenticated.json()
    assert await context.db.surveys.find_one({"_id": survey["id"]}) == before
    assert await context.db.survey_versions.count_documents({"surveyId": survey["id"]}) == 0
    assert await context.db.responses.count_documents({"surveyId": survey["id"]}) == 0
