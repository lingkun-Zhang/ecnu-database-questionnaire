"""Version-scoped MongoDB aggregations over an immutable submission range."""

from __future__ import annotations

from typing import Any, Literal

from pymongo import ReadPreference
from pymongo.read_concern import ReadConcern
from pymongo.write_concern import WriteConcern


RespondentType = Literal["all", "anonymous", "named"]


class StatsError(ValueError):
    def __init__(self, code: str, message: str, status_code: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def build_response_scope(
    survey_id: str, version_id: str, as_of: int, respondent_type: RespondentType = "all"
) -> dict[str, Any]:
    """Share the submission boundary and identity-mode filter across owner reads."""
    if respondent_type not in ("all", "anonymous", "named"):
        raise StatsError("INVALID_RESPONDENT_TYPE", "提交方式必须为 all、anonymous 或 named", 422)
    query: dict[str, Any] = {
        "surveyId": survey_id,
        "versionId": version_id,
        "acceptedSeq": {"$lte": as_of},
    }
    if respondent_type != "all":
        query["anonymous"] = respondent_type == "anonymous"
    return query


def build_stats_pipeline(
    survey_id: str, version_id: str, as_of: int, question_ids: list[str],
    respondent_type: RespondentType = "all",
) -> list[dict[str, Any]]:
    """Produce bounded summaries; raw text and participant identities never leave MongoDB.

    U and S are measured using independent set differences, not manufactured by
    subtracting A/V from R. Participant grouping emits counts rather than arrays.
    """
    answer_rows = [
        {"$unwind": "$answers"},
        {"$match": {"answers.questionId": {"$in": question_ids}}},
    ]
    return [
        {"$match": build_response_scope(survey_id, version_id, as_of, respondent_type)},
        {
            "$facet": {
                "overview": [
                    {"$group": {
                        "_id": None,
                        "totalResponses": {"$sum": 1},
                        "anonymousResponses": {"$sum": {"$cond": ["$anonymous", 1, 0]}},
                        "namedResponses": {"$sum": {"$cond": ["$anonymous", 0, 1]}},
                    }}
                ],
                "participants": [{"$group": {"_id": "$participantKey"}}, {"$count": "count"}],
                "reached": [
                    {"$unwind": "$path"},
                    {"$match": {"path": {"$in": question_ids}}},
                    {"$group": {"_id": "$path", "count": {"$sum": 1}}},
                ],
                "unanswered": [
                    {"$project": {"ids": {"$setIntersection": [
                        {"$setDifference": ["$path", "$answers.questionId"]},
                        {"$literal": question_ids},
                    ]}}},
                    {"$unwind": "$ids"},
                    {"$group": {"_id": "$ids", "count": {"$sum": 1}}},
                ],
                "skipped": [
                    {"$project": {"ids": {"$setDifference": [{"$literal": question_ids}, "$path"]}}},
                    {"$unwind": "$ids"},
                    {"$group": {"_id": "$ids", "count": {"$sum": 1}}},
                ],
                "answered": [
                    *answer_rows,
                    {"$group": {
                        "_id": "$answers.questionId",
                        "count": {"$sum": 1},
                        "sum": {"$sum": {"$cond": [{"$eq": ["$answers.kind", "number"]}, "$answers.value", 0]}},
                        "avg": {"$avg": {"$cond": [{"$eq": ["$answers.kind", "number"]}, "$answers.value", None]}},
                    }},
                ],
                "answeredParticipants": [
                    *answer_rows,
                    {"$group": {"_id": {"questionId": "$answers.questionId", "participant": "$participantKey"}}},
                    {"$group": {"_id": "$_id.questionId", "count": {"$sum": 1}}},
                ],
                "options": [
                    *answer_rows,
                    {"$match": {"answers.kind": {"$in": ["single", "multi"]}}},
                    {"$project": {
                        "questionId": "$answers.questionId",
                        "participantKey": 1,
                        "selections": {"$cond": [
                            {"$eq": ["$answers.kind", "multi"]},
                            "$answers.value",
                            ["$answers.value"],
                        ]},
                    }},
                    {"$unwind": "$selections"},
                    {"$group": {
                        "_id": {"questionId": "$questionId", "optionId": "$selections", "participant": "$participantKey"},
                        "count": {"$sum": 1},
                    }},
                    {"$group": {
                        "_id": {"questionId": "$_id.questionId", "optionId": "$_id.optionId"},
                        "count": {"$sum": "$count"},
                        "uniqueParticipants": {"$sum": 1},
                    }},
                ],
            }
        },
    ]


def format_stats(
    survey: dict[str, Any],
    version: dict[str, Any],
    as_of: int,
    aggregated: dict[str, Any],
    question_id: str | None = None,
) -> dict[str, Any]:
    """Apply labels/zero options from the immutable definition to bounded counts."""
    overview = next(iter(aggregated.get("overview", [])), {})
    total = int(overview.get("totalResponses", 0))
    indexes = {
        field: {row["_id"]: row for row in aggregated.get(field, [])}
        for field in ("reached", "unanswered", "skipped", "answered", "answeredParticipants")
    }
    options = {
        (row["_id"]["questionId"], row["_id"]["optionId"]): row
        for row in aggregated.get("options", [])
    }
    questions = []
    for question in version["definition"]["questions"]:
        qid = question["id"]
        if question_id is not None and qid != question_id:
            continue
        answered = indexes["answered"].get(qid, {})
        reached = int(indexes["reached"].get(qid, {}).get("count", 0))
        count = int(answered.get("count", 0))
        unanswered = int(indexes["unanswered"].get(qid, {}).get("count", 0))
        skipped = int(indexes["skipped"].get(qid, {}).get("count", 0))
        if not (0 <= count <= reached <= total and total == count + unanswered + skipped):
            raise StatsError("DATA_INVARIANT", "答卷路径与答案违反统计一致性约束", 500)
        row: dict[str, Any] = {
            "questionId": qid,
            "kind": question["kind"],
            "title": question["title"],
            "R": total,
            "V": reached,
            "A": count,
            "U": unanswered,
            "S": skipped,
            "answeredResponses": count,
            "answeredParticipants": int(indexes["answeredParticipants"].get(qid, {}).get("count", 0)),
        }
        row["uniqueParticipants"] = row["answeredParticipants"]
        if question["kind"] in ("single", "multi"):
            row["options"] = []
            for option in question["config"]["options"]:
                value = options.get((qid, option["id"]), {})
                option_count = int(value.get("count", 0))
                if option_count > count:
                    raise StatsError("DATA_INVARIANT", "单个选项次数超过有效答卷数", 500)
                row["options"].append({
                    "id": option["id"],
                    "label": option["label"],
                    "count": option_count,
                    "uniqueParticipants": int(value.get("uniqueParticipants", 0)),
                    "percentage": option_count * 100.0 / count if count else 0.0,
                })
            if question["kind"] == "single" and sum(item["count"] for item in row["options"]) != count:
                raise StatsError("DATA_INVARIANT", "单选分布与有效答卷数不一致", 500)
            row["totalSelections"] = sum(item["count"] for item in row["options"])
        elif question["kind"] == "number":
            row["sum"] = answered.get("sum", 0)
            row["avg"] = answered.get("avg") if count else None
        questions.append(row)
    return {
        "surveyId": survey["_id"],
        "versionId": version["_id"],
        "versionNo": version["versionNo"],
        "asOfSeq": as_of,
        "totalResponses": total,
        "uniqueParticipants": int(next(iter(aggregated.get("participants", [])), {}).get("count", 0)),
        "anonymousResponses": int(overview.get("anonymousResponses", 0)),
        "namedResponses": int(overview.get("namedResponses", 0)),
        "questions": questions,
    }


async def get_stats(
    db: Any,
    survey: dict[str, Any],
    version: dict[str, Any],
    as_of: int | None = None,
    question_id: str | None = None,
    respondent_type: RespondentType = "all",
) -> dict[str, Any]:
    """Read the sequence and all aggregates from the same snapshot transaction.

    The API authenticates the owner before calling this function. Ownership is
    rechecked in the snapshot when ownerId is present on the supplied survey.
    """
    if version.get("surveyId") != survey.get("_id"):
        raise StatsError("NOT_FOUND", "发布版本不属于此问卷", 404)
    if as_of is not None and (type(as_of) is not int or as_of < 0):
        raise StatsError("INVALID_AS_OF", "asOfSeq 必须为非负整数")
    questions = version["definition"]["questions"]
    if question_id is not None and not any(q["id"] == question_id for q in questions):
        raise StatsError("NOT_FOUND", "此发布版本不存在该题目", 404)
    wanted = [q["id"] for q in questions if question_id is None or q["id"] == question_id]

    async def read_snapshot(session: Any) -> dict[str, Any]:
        lookup = {"_id": survey["_id"]}
        if "ownerId" in survey:
            lookup["ownerId"] = survey["ownerId"]
        current = await db.surveys.find_one(lookup, {"submitSequence": 1}, session=session)
        if current is None:
            raise StatsError("NOT_FOUND", "问卷不存在或无访问权限", 404)
        sequence = int(current["submitSequence"])
        boundary = sequence if as_of is None else as_of
        if boundary > sequence:
            raise StatsError("INVALID_AS_OF", "asOfSeq 不能超过当前已提交序号")
        cursor = await db.responses.aggregate(
            build_stats_pipeline(survey["_id"], version["_id"], boundary, wanted, respondent_type),
            session=session,
            maxTimeMS=30000,
        )
        result = await cursor.to_list(length=1)
        formatted = format_stats(survey, version, boundary, result[0] if result else {}, question_id)
        formatted["respondentType"] = respondent_type
        return formatted

    async with db.client.start_session() as session:
        return await session.with_transaction(
            read_snapshot,
            read_concern=ReadConcern("snapshot"),
            write_concern=WriteConcern("majority"),
            read_preference=ReadPreference.PRIMARY,
        )
