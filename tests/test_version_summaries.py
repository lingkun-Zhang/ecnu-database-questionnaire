"""History summaries through real owner-authenticated API and MongoDB."""
from copy import deepcopy
from uuid import uuid4

from conftest import account, published
from scripts.fixtures import example_definition
from test_question_statistics import source_survey
from test_question_versions import accepted, create_question, number_content
from test_question_sources import snapshot, unchanged


async def append_version(context, owner, survey, title):
    definition = example_definition()
    definition['title'] = title
    saved = await context.client.put(f'/api/surveys/{survey["id"]}/draft', headers=owner,
        json={'expectedDraftRevision': survey['draftRevision'], 'definition': definition})
    assert saved.status_code == 200, saved.text
    result = await context.client.post(f'/api/surveys/{survey["id"]}/publish', headers=owner,
        json={'expectedDraftRevision': saved.json()['draftRevision'],
              'expectedCurrentVersionId': survey['currentVersionId'], 'publishKey': str(uuid4())})
    assert result.status_code == 201, result.text
    latest = await context.client.get(f'/api/surveys/{survey["id"]}', headers=owner)
    return latest.json(), result.json()


async def test_history_summaries_are_small_owner_only_pages_and_legacy_full_is_unchanged(context):
    owner, _ = await account(context)
    other, _ = await account(context, 'history_other')
    survey, first = await published(context, owner, example_definition())
    survey, second = await append_version(context, owner, survey, '第二版的真实标题')
    survey, third = await append_version(context, owner, survey, '第三版的真实标题')
    before = await snapshot(context)
    base = f'/api/surveys/{survey["id"]}/versions'
    full = await context.client.get(base, headers=owner)
    assert full.status_code == 200
    assert full.json()['items'] == [third, second, first]
    first_page = await context.client.get(base, headers=owner, params={'summary': 'true', 'limit': 1})
    second_page = await context.client.get(base, headers=owner, params={'summary': 'true', 'limit': 1, 'offset': 1})
    last_page = await context.client.get(base, headers=owner, params={'summary': 'true', 'offset': 2})
    for result, version in [(first_page, third), (second_page, second), (last_page, first)]:
        assert result.status_code == 200, result.text
        assert result.json()['items'] == [{key: version[key] for key in
            ('id', 'surveyId', 'versionNo', 'sourceDraftRevision', 'publishedAt')} | {'title': version['definition']['title']}]
        assert 'definition' not in result.text and 'questionSources' not in result.text
    assert (await context.client.get(base, headers=other, params={'summary': 'true'})).status_code == 404
    assert (await context.client.get(base, params={'summary': 'true'})).status_code == 401
    assert (await context.client.get(base, headers=owner, params={'summary': 'invalid'})).status_code == 422
    detail = await context.client.get(f'{base}/{first["id"]}', headers=owner)
    assert detail.json() == first
    await unchanged(context, before)


async def test_sourced_history_summary_omits_sources_but_full_detail_keeps_fixed_binding(context):
    owner, _ = await account(context)
    question = accepted(await create_question(context, owner, number_content('仅用于固定来源')), 1)
    survey, version = await source_survey(context, owner, question)
    before = await snapshot(context)
    base = f'/api/surveys/{survey["id"]}/versions'
    summary = await context.client.get(base, headers=owner, params={'summary': 'true'})
    assert summary.status_code == 200, summary.text
    assert summary.json()['items'][0]['title'] == version['definition']['title']
    assert 'questionSources' not in summary.text and 'definition' not in summary.text
    detail = await context.client.get(f'{base}/{version["id"]}', headers=owner)
    assert detail.json()['questionSources'] == version['questionSources']
    assert detail.json()['definition'] == version['definition']
    await unchanged(context, before)


async def test_summary_database_projection_does_not_fetch_full_definition(context, monkeypatch):
    owner, _ = await account(context)
    survey, _ = await published(context, owner, example_definition())
    collection_type = type(context.db.survey_versions)
    original = collection_type.find
    projections = []

    def observe(collection, query=None, *args, **kwargs):
        if collection.database.name == context.settings.database and collection.name == 'survey_versions' and query == {'surveyId': survey['id']}:
            projections.append(deepcopy(args[0] if args else kwargs.get('projection')))
        return original(collection, query, *args, **kwargs)

    monkeypatch.setattr(collection_type, 'find', observe)
    result = await context.client.get(f'/api/surveys/{survey["id"]}/versions', headers=owner, params={'summary': 'true'})
    assert result.status_code == 200, result.text
    assert len(projections) == 1 and projections[0]
    projection = projections[0]
    assert projection.get('definition.title') == 1 and not projection.get('definition')
    assert not projection.get('questionSources')
