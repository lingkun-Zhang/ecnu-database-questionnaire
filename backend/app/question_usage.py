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
