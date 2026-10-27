"""Read explicit provenance with bounded, owner-scoped container pagination.

Query builders are shared with the reproducible query-plan experiment. They do
not infer identity from content and do not introduce a second usage registry.
"""


DRAFT_USAGE_PROJECTION = {
    "_id": 1, "draft.title": 1, "draftRevision": 1, "status": 1,
    "draftQuestionSources": 1,
}


def source_filter(field, question_id, version_id=None, after_id=None):
    match = {"questionId": question_id}
    if version_id is not None:
        match["questionVersionId"] = version_id
    query = {field: {"$elemMatch": match}}
    if after_id is not None:
        query["_id"] = {"$gt": after_id}
    return query


def draft_usage_filter(owner_id, question_id, version_id=None, after_id=None):
    return {"ownerId": owner_id,
            **source_filter("draftQuestionSources", question_id, version_id, after_id)}


def published_usage_pipeline(owner_id, question_id, version_id=None, after_id=None, limit=30):
    return [
        {"$match": source_filter("questionSources", question_id, version_id, after_id)},
        {"$sort": {"_id": 1}},
        # Published snapshots have no redundant owner field. Resolve the real
        # survey owner before limiting, including when foreign rows sort first.
        {"$lookup": {"from": "surveys", "localField": "surveyId", "foreignField": "_id",
                     "pipeline": [{"$match": {"ownerId": owner_id}},
                                  {"$project": {"status": 1, "currentVersionId": 1}}],
                     "as": "usageSurvey"}},
        {"$unwind": "$usageSurvey"},
        {"$limit": limit + 1},
        {"$project": {"surveyId": 1, "definition.title": 1, "versionNo": 1,
                      "publishedAt": 1, "questionSources": 1,
                      "surveyStatus": "$usageSurvey.status",
                      "isCurrent": {"$eq": ["$_id", "$usageSurvey.currentVersionId"]}}},
    ]


def matching_instances(sources, question_id, version_id=None):
    return [{"instanceId": source["instanceId"], "questionVersionId": source["questionVersionId"]}
            for source in sources if source["questionId"] == question_id
            and (version_id is None or source["questionVersionId"] == version_id)]


IMPACT_SUMMARY_FIELDS = (
    "surveyCount", "draftSurveyCount", "draftInstanceCount", "publishedSurveyCount",
    "currentPublishedSurveyCount", "currentPublishedInstanceCount",
    "historicalSnapshotCount", "historicalInstanceCount",
)
IMPACT_ITEM_COUNTS = (
    "draftInstanceCount", "currentPublishedInstanceCount",
    "historicalSnapshotCount", "historicalInstanceCount",
)


def _impact_instance_count(field, question_id, version_id):
    conditions = [{"$eq": ["$$reference.questionId", {"$literal": question_id}]}]
    if version_id is not None:
        conditions.append({"$eq": ["$$reference.questionVersionId", {"$literal": version_id}]})
    return {"$size": {"$filter": {"input": {"$ifNull": ["$" + field, []]},
                                 "as": "reference", "cond": {"$and": conditions}}}}


def question_impact_pipeline(owner_id, question_id, version_id=None, offset=0, limit=20):
    """Count saved references, keeping one metadata row per extant survey.

    Starting from surveys deliberately excludes orphan publication snapshots.
    Both the totals and the requested page consume the same facet input. This
    is an advisory live query, not a persistent range or a global update lock.
    """
    is_current = {"$eq": ["$_id", "$$currentVersionId"]}
    positive = lambda field: {"$cond": [{"$gt": ["$" + field, 0]}, 1, 0]}
    summary = {"_id": "$ownership", "surveyCount": {"$sum": 1},
               "draftSurveyCount": {"$sum": positive("draftInstanceCount")},
               "publishedSurveyCount": {"$sum": positive("publishedSnapshotCount")},
               "currentPublishedSurveyCount": {"$sum": positive("currentPublishedInstanceCount")}}
    for field in ("draftInstanceCount", "currentPublishedInstanceCount",
                  "historicalSnapshotCount", "historicalInstanceCount"):
        summary[field] = {"$sum": "$" + field}
    item_projection = {"_id": 1, "ownership": 1, **{field: 1 for field in IMPACT_ITEM_COUNTS},
        "surveyTitle": {"$cond": [{"$eq": ["$ownership", "own"]}, "$draft.title", "$$REMOVE"]},
        "surveyStatus": {"$cond": [{"$eq": ["$ownership", "own"]}, "$status", "$$REMOVE"]}}
    return [
        {"$project": {"_id": 1, "draft.title": 1, "status": 1, "currentVersionId": 1,
            "ownership": {"$cond": [{"$eq": ["$ownerId", {"$literal": owner_id}]}, "own", "other"]},
            "draftInstanceCount": _impact_instance_count("draftQuestionSources", question_id, version_id)}},
        {"$lookup": {"from": "survey_versions", "localField": "_id", "foreignField": "surveyId",
            "let": {"currentVersionId": "$currentVersionId"}, "pipeline": [
                {"$match": source_filter("questionSources", question_id, version_id)},
                {"$project": {"_id": 1, "instanceCount": _impact_instance_count("questionSources", question_id, version_id)}},
                {"$group": {"_id": None, "publishedSnapshotCount": {"$sum": 1},
                    "currentPublishedInstanceCount": {"$sum": {"$cond": [is_current, "$instanceCount", 0]}},
                    "historicalSnapshotCount": {"$sum": {"$cond": [is_current, 0, 1]}},
                    "historicalInstanceCount": {"$sum": {"$cond": [is_current, 0, "$instanceCount"]}}}},
            ], "as": "publicationCounts"}},
        {"$set": {"publicationCounts": {"$ifNull": [{"$arrayElemAt": ["$publicationCounts", 0]}, {}]}}},
        {"$set": {field: {"$ifNull": ["$publicationCounts." + field, 0]}
            for field in ("publishedSnapshotCount", "currentPublishedInstanceCount",
                          "historicalSnapshotCount", "historicalInstanceCount")}},
        {"$match": {"$or": [{"draftInstanceCount": {"$gt": 0}}, {"publishedSnapshotCount": {"$gt": 0}}]}},
        {"$facet": {"summary": [{"$group": summary}],
            "items": [{"$sort": {"_id": 1}}, {"$skip": offset}, {"$limit": limit + 1},
                      {"$project": item_projection}]}},
    ]


def question_impact_view(data, question_id, version_id, checked_at, secret, offset, limit):
    # Reuse the structured HMAC primitive with a separate purpose namespace.
    # The alias is metadata, never an authorization or survey navigation token.
    from .security import participant_key

    summary = {name: dict.fromkeys(IMPACT_SUMMARY_FIELDS, 0) for name in ("own", "others")}
    for row in data.get("summary", []):
        category = "own" if row["_id"] == "own" else "others"
        summary[category] = {field: row[field] for field in IMPACT_SUMMARY_FIELDS}
    rows = data.get("items", [])
    items = []
    for row in rows[:limit]:
        item = {"reference": "问卷引用 " + participant_key(secret, "question-impact:" + question_id, row["_id"])[:20],
                "ownership": row["ownership"], **{field: row[field] for field in IMPACT_ITEM_COUNTS}}
        if row["ownership"] == "own":
            item.update(surveyId=row["_id"], surveyTitle=row["surveyTitle"], surveyStatus=row["surveyStatus"])
        items.append(item)
    return {"questionId": question_id, "questionVersionId": version_id, "checkedAt": checked_at,
            "scope": "saved_references", "summary": summary, "items": items,
            "offset": offset, "limit": limit,
            "nextOffset": offset + limit if len(rows) > limit else None}
