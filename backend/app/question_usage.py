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



async def impact_candidate_batches(db, question_id, version_id, session, batch_size=100):
    """Merge sorted reference IDs without a global array or transaction union.

    Publications can outlive a malformed/orphan survey; the later real-survey
    aggregate, not this candidate stream, decides which references count.
    """
    drafts = db.surveys.find(source_filter("draftQuestionSources", question_id, version_id),
        {"_id": 1}, session=session).sort("_id", 1).batch_size(batch_size).max_time_ms(10000)
    publications = await db.survey_versions.aggregate([
        {"$match": source_filter("questionSources", question_id, version_id)},
        {"$group": {"_id": "$surveyId"}}, {"$sort": {"_id": 1}}],
        session=session, batchSize=batch_size, maxTimeMS=10000)
    try:
        draft = await anext(drafts, None)
        publication = await anext(publications, None)
        batch = []
        while draft is not None or publication is not None:
            identifier = min(row["_id"] for row in (draft, publication) if row is not None)
            batch.append(identifier)
            if draft is not None and draft["_id"] == identifier:
                draft = await anext(drafts, None)
            if publication is not None and publication["_id"] == identifier:
                publication = await anext(publications, None)
            if len(batch) == batch_size:
                yield batch
                batch = []
        if batch:
            yield batch
    finally:
        await drafts.close()
        await publications.close()


async def read_question_impact(db, owner_id, question_id, version_id, offset, limit, session):
    """Keep exact totals and global ID paging while visiting only candidates."""
    summaries, items, position = {}, [], 0
    async for identifiers in impact_candidate_batches(db, question_id, version_id, session):
        # Each ID list is bounded. A completed page needs no further titles,
        # but all candidates still contribute to the accurate summary.
        collect_items = len(items) <= limit
        pipeline = [{"$match": {"_id": {"$in": identifiers}}}, *question_impact_pipeline(
            owner_id, question_id, version_id, 0, len(identifiers) if collect_items else 0)]
        rows = await (await db.surveys.aggregate(pipeline, session=session, maxTimeMS=10000)).to_list(length=1)
        data = rows[0] if rows else {}
        count = 0
        for row in data.get("summary", []):
            total = summaries.setdefault(row["_id"], {"_id": row["_id"],
                **dict.fromkeys(IMPACT_SUMMARY_FIELDS, 0)})
            for field in IMPACT_SUMMARY_FIELDS:
                total[field] += row[field]
            count += row["surveyCount"]
        if collect_items:
            for index, row in enumerate(data.get("items", [])):
                if position + index >= offset and len(items) <= limit:
                    items.append(row)
        position += count
    return {"summary": list(summaries.values()), "items": items}


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
