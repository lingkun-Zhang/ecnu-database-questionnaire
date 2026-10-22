"""Real browser fixture for owner close, wall-clock deadline and reader feedback.

Seed two published one-question surveys with one existing answer each. Browser
performs owner changes and fill interactions. Audit real responses and full BSON;
never alter requests, responses or the service clock. Only own random DB is dropped.
"""
from __future__ import annotations
import argparse
import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import secrets
import socket
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / 'backend'), str(Path(__file__).parent)]
import httpx
import uvicorn
from app.config import Settings
from app.main import create_app
from combination_browser import snapshot, fingerprint

USERNAME = 'availability_browser'
PASSWORD = 'AvailabilityBrowser2026!'  # Disposable local test account.


def definition(label):
    return {'title': f'{label}页面验收', 'description': '已有一份历史答卷；验证停止收集后不再新增。',
            'allowAnonymous': True, 'entryQuestionId': 'q_number', 'questions': [
        {'id': 'q_number', 'kind': 'number', 'title': '请输入0至100的数字', 'required': True,
         'config': {'min': 0, 'max': 100, 'integerOnly': True}, 'defaultTarget': 'END', 'rules': []}]}


class AvailabilityAudit:
    def __init__(self, app, output):
        self.app, self.output, self.baseline = app, output, None
        self.state = {'events': [], 'cleanedUp': False, 'cases': {}}

    def save(self):
        temporary = self.output / 'state.tmp'
        temporary.write_text(json.dumps(self.state, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(self.output / 'state.json')

    async def observe(self, scope, body, status, result, request_at):
        path, method = scope['path'], scope['method']
        case, info = next((label, info) for label, info in self.state['cases'].items()
                          if path.startswith(info['managementPath']) or path.startswith(info['publicPath']))
        after = await snapshot(self.app.state.db)
        previous = next(s for s in self.baseline['surveys'] if s['_id'] == info['surveyId'])
        current = next(s for s in after['surveys'] if s['_id'] == info['surveyId'])
        endpoint = path.rsplit('/', 1)[-1]
        checks = {}
        if method in ('PATCH', 'POST') and endpoint in ('settings', 'close'):
            expected = deepcopy(self.baseline)
            target = next(s for s in expected['surveys'] if s['_id'] == info['surveyId'])
            checks.update(http200=status == 200, revisionMatches=body['expectedSettingsRevision'] == previous['settingsRevision'])
            target.update(settingsRevision=previous['settingsRevision'] + 1, updatedAt=current['updatedAt'])
            if endpoint == 'close':
                target['status'] = 'closed'
                checks['onlyCloseCase'] = case == 'close' and previous['status'] == 'open'
            else:
                wanted = datetime.fromisoformat(body['deadlineAt'].replace('Z', '+00:00')) if body['deadlineAt'] else None
                target['deadlineAt'] = wanted
                checks.update(onlyDeadlineCase=case == 'deadline', onlyDeadlineSetting=set(body) == {'expectedSettingsRevision','deadlineAt'},
                              futureWhenSaved=wanted is not None and wanted > request_at,
                              deadlineStoredCorrectly=current['deadlineAt'] == wanted)
                self.state['deadlineAt'] = wanted.isoformat() if wanted else None
            checks['onlyExpectedFieldsChanged'] = after == expected
            checks['responseMatches'] = result['settingsRevision'] == current['settingsRevision'] and result['status'] == current['status']
            self.baseline = after
            self.state['lastSnapshotSha256'] = fingerprint(after)
        else:
            checks['readOrRejectedActionDidNotWrite'] = after == self.baseline
            unavailable = 'closed' if previous['status'] == 'closed' else 'expired' if previous['deadlineAt'] and request_at >= previous['deadlineAt'] else None
            if method == 'POST' and endpoint in ('start','evaluate','submit'):
                expected_status = 409 if unavailable else 200
                checks['statusMatchesAvailability'] = status == expected_status
                if unavailable:
                    checks['errorCodeMatches'] = result.get('code') == {'closed':'SURVEY_CLOSED','expired':'SURVEY_EXPIRED'}[unavailable]
                elif endpoint == 'evaluate' and body['answers'].get('q_number') is not None:
                    checks['validAnswerComplete'] = result['complete'] and not result['errors']
            elif method == 'GET' and path == info['publicPath']:
                checks['publicStatusMatches'] = status == 200 and result['status'] == (unavailable or 'open')
            elif method == 'GET' and endpoint == 'stats':
                question = result['questions'][0]
                checks['historyStatsPreserved'] = status == 200 and result['totalResponses'] == 1 and question['avg'] == info['existingValue']
            else:
                checks['http200'] = status == 200
        event = {'case': case, 'method': method, 'endpoint': endpoint, 'httpStatus': status,
                 'requestAt': request_at.isoformat(), 'observedAt': datetime.now(timezone.utc).isoformat(),
                 'code': result.get('code') if isinstance(result, dict) else None, 'checks': checks}
        self.state['events'].append(event)
        self.state['allObservedChecksPassed'] = all(all(event['checks'].values()) for event in self.state['events'])
        self.save()

    async def __call__(self, scope, receive, send):
        cases = self.state['cases'].values()
        if scope['type'] != 'http' or not any(scope.get('path','').startswith(info['managementPath']) or scope.get('path','').startswith(info['publicPath']) for info in cases):
            return await self.app(scope, receive, send)
        body, messages = bytearray(), []
        request_at = datetime.now(timezone.utc)
        async def capture_receive():
            message = await receive()
            if message['type'] == 'http.request': body.extend(message.get('body', b''))
            return message
        async def capture_send(message): messages.append(message)
        await self.app(scope, capture_receive, capture_send)
        try:
            status = next(message['status'] for message in messages if message['type'] == 'http.response.start')
            response = b''.join(message.get('body', b'') for message in messages if message['type'] == 'http.response.body')
            await self.observe(scope, json.loads(body) if body else {}, status, json.loads(response), request_at)
        except Exception as error:
            self.state['auditErrorType'] = type(error).__name__
            self.save()
        for message in messages: await send(message)


async def run(output):
    output.mkdir(parents=True, exist_ok=False)
    database = 'survey_test_' + uuid4().hex
    settings = replace(Settings.from_env(), database=database, testing=True, auth_secret=secrets.token_hex(32),
                       fill_secret=secrets.token_hex(32), participant_secret=secrets.token_hex(32))
    app = create_app(settings)
    audit = AvailabilityAudit(app, output)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(('127.0.0.1', 0))
    origin = f'http://127.0.0.1:{listener.getsockname()[1]}'
    original_lifespan = app.router.lifespan_context
    @asynccontextmanager
    async def lifespan(application):
        async with original_lifespan(application):
            try:
                case_info = {}
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url='http://fixture') as client:
                    result = await client.post('/api/auth/register', json={'username': USERNAME, 'password': PASSWORD})
                    assert result.status_code == 201
                    headers = {'Authorization': 'Bearer ' + result.json()['accessToken']}
                    for label, title, value in [('close','关闭',10), ('deadline','截止',20)]:
                        result = await client.post('/api/surveys', headers=headers, json={'title': definition(title)['title']})
                        assert result.status_code == 201
                        survey = result.json()
                        path, public = '/api/surveys/' + survey['id'], '/api/survey/' + survey['publicId']
                        result = await client.patch(path + '/settings', headers=headers, json={'expectedSettingsRevision':0,'allowMultiple':True})
                        assert result.status_code == 200
                        result = await client.put(path + '/draft', headers=headers, json={'expectedDraftRevision':0,'definition':definition(title)})
                        assert result.status_code == 200
                        result = await client.post(path + '/publish', headers=headers, json={'expectedDraftRevision':1,'expectedCurrentVersionId':None,'publishKey':str(uuid4())})
                        assert result.status_code == 201
                        version = result.json()['id']
                        result = await client.post(public + '/start', headers=headers)
                        assert result.status_code == 200
                        result = await client.post(public + '/submit', headers={**headers,'X-Fill-Token':result.json()['fillToken'],'Idempotency-Key':str(uuid4())},
                            json={'versionId':version,'anonymous':True,'answers':{'q_number':value}})
                        assert result.status_code == 201
                        case_info[label] = {'surveyId':survey['id'],'managementPath':path,'publicPath':public,
                                            'fillURL': origin + '/survey/' + survey['publicId'], 'existingValue':value}
                audit.baseline = await snapshot(application.state.db)
                audit.state.update(origin=origin, username=USERNAME, cases=case_info,
                    baselineCounts={name:len(items) for name,items in audit.baseline.items()},
                    baselineSha256=fingerprint(audit.baseline), lastSnapshotSha256=fingerprint(audit.baseline))
                audit.save()
                print('AVAILABILITY_BROWSER_READY ' + json.dumps({'origin':origin,'cases':case_info},ensure_ascii=False),flush=True)
                yield
            finally:
                if audit.baseline is not None:
                    audit.state['finalSnapshotMatches'] = await snapshot(application.state.db) == audit.baseline
                assert re.fullmatch(r'survey_test_[0-9a-f]{32}', database) and settings.database == database
                await application.state.db.client.drop_database(database)
                audit.state['cleanedUp'] = True
                audit.state['databaseAbsentAfterCleanup'] = database not in await application.state.db.client.list_database_names()
                audit.save()
    app.router.lifespan_context = lifespan
    server = uvicorn.Server(uvicorn.Config(audit, log_level='warning', access_log=False))
    async def watch_stop():
        while not server.should_exit:
            if (output / 'stop.request').exists(): server.should_exit = True; return
            await asyncio.sleep(.25)
    watcher = asyncio.create_task(watch_stop())
    try: await server.serve(sockets=[listener])
    finally:
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)
        listener.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    asyncio.run(run(parser.parse_args().output.resolve()))
