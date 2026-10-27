"""Rehearse two real application versions using four owned disposable databases.

No dotenv/configuration fallback, existing database argument, production data or
network HTTP server. Raw BSON backups and synthetic credentials stay in a new
private .runtime directory. This is a rehearsal, not a deployment/migration tool.
"""
from __future__ import annotations
import argparse
import asyncio
from copy import deepcopy
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import shutil
import subprocess
import sys
from uuid import uuid4
import zipfile

BASELINE='99b66d1e637fa3b4ecb13719c0230ecb7e958701'
ROOT=Path(__file__).resolve().parents[1]
OLD=('users','surveys','survey_versions','responses')
NEW=('questions','question_versions','question_shares','question_banks','question_bank_items','question_stat_scopes','question_stat_sources','question_stat_grants')
MARKER='upgrade_rehearsal_ownership'

def validate_uri(uri):
    from pymongo.uri_parser import parse_uri
    parsed=parse_uri(uri)
    if (parsed['nodelist'] != [('127.0.0.1',27227)] or parsed.get('username') or parsed.get('password')
        or parsed.get('database') or parsed['options'].get('replicaSet')!='phase2test'):
        raise ValueError('An explicit credential-free loopback test replica set on port27227 is required')
    return uri

def validate_database(name):
    if not re.fullmatch(r'survey_upgrade_(?:seed|upgraded|mixed|restored)_[a-f0-9]{32}',name):
        raise ValueError('Not an owned rehearsal database name')
    return name

def check_marker(marker,run_id,stage):
    if marker!={'_id':'owner','runId':run_id,'stage':stage}:
        raise RuntimeError('Rehearsal ownership mismatch; retain database')

def validate_output(path,root=ROOT):
    path=Path(path).resolve();runtime=(root/'.runtime').resolve()
    if not path.is_relative_to(runtime) or path==runtime or path.exists():
        raise ValueError('Output must be a new private directory inside .runtime')
    return path

def archive_members(names):
    for name in names:
        normalized=name.replace('\\','/')
        p=PurePosixPath(normalized)
        if p.is_absolute() or '..' in p.parts or ':' in normalized or (p.parts and p.parts[0] not in ('backend','requirements.txt')):
            raise ValueError('Unsafe source archive member')
    return names

def dump(path,value):
    Path(path).write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')

def read(path):return json.loads(Path(path).read_text(encoding='utf-8'))

def fingerprint(value):
    from bson.json_util import dumps, CANONICAL_JSON_OPTIONS
    text=dumps(value,json_options=CANONICAL_JSON_OPTIONS)
    return hashlib.sha256(json.dumps(json.loads(text),sort_keys=True,separators=(',',':')).encode()).hexdigest()

def export_backup(client,database,path,collections):
    from bson import BSON
    from pymongo.read_concern import ReadConcern
    from pymongo.write_concern import WriteConcern
    path.mkdir(exist_ok=False)
    db=client[database];result={}
    metadata={r['name']:r['options'] for r in db.list_collections() if r['name'] in collections}
    with client.start_session() as session:
        with session.start_transaction(read_concern=ReadConcern('snapshot'),write_concern=WriteConcern('majority')):
            for name in collections:
                docs=list(db[name].find({},session=session).sort('_id',1))
                indexes=list(db[name].list_indexes())
                (path/(name+'.bson')).write_bytes(b''.join(BSON.encode(d) for d in docs))
                (path/(name+'.meta.bson')).write_bytes(BSON.encode({'options':metadata[name],'indexes':indexes}))
                result[name]={'count':len(docs),'documentsSha256':fingerprint(docs),'optionsSha256':fingerprint(metadata[name])}
    dump(path/'manifest.json',result)
    return result

def restore_backup(client,database,path,collections):
    from bson import decode_all
    from pymongo import IndexModel
    db=client[database]
    for name in collections:
        if name in db.list_collection_names():raise RuntimeError('Restore target must be empty apart from marker')
        meta=decode_all((path/(name+'.meta.bson')).read_bytes())[0]
        db.create_collection(name,**meta['options'])
        docs=decode_all((path/(name+'.bson')).read_bytes())
        if docs:db[name].insert_many(docs)
        models=[]
        for ix in meta['indexes']:
            if ix['name']=='_id_':continue
            models.append(IndexModel(list(ix['key'].items()),**{k:v for k,v in ix.items() if k not in ('key','v','ns')}))
        if models:db[name].create_indexes(models)

def document_hashes(db,collections):
    return {name:fingerprint(list(db[name].find({}).sort('_id',1))) for name in collections}

def index_contract(index):
    return {**{k:v for k,v in index.items() if k not in ('key','v','ns')},'key':list(index['key'].items())}

async def worker(action,code,database,output):
    # Each worker is a fresh process. Never import both app generations together.
    import dotenv
    dotenv.load_dotenv=lambda *a,**kw:False
    sys.path.insert(0,str(code/'backend'))
    import httpx
    from asgi_lifespan import LifespanManager
    from app.config import Settings
    from app.main import create_app
    import app.main
    assert Path(app.main.__file__).resolve().is_relative_to(code.resolve())
    from pymongo import MongoClient
    from dataclasses import replace
    settings=replace(Settings.from_env(),database=database,testing=True)
    validate_uri(settings.mongo_uri);validate_database(database)
    state_path=output/'synthetic-state.private.json'
    state=read(state_path) if state_path.exists() else {'password':secrets.token_urlsafe(24)}
    checks={'action':action,'applicationSourceVerified':True}
    control=MongoClient(settings.mongo_uri,serverSelectionTimeoutMS=5000);db=control[database]
    owner_manifest=read(output/'ownership.private.json')
    stage=next(k for k,v in owner_manifest['databases'].items() if v==database)
    check_marker(db[MARKER].find_one({'_id':'owner'}),owner_manifest['runId'],stage)
    before=document_hashes(db,OLD+NEW) if action in ('mixed-old','repair-current') else None
    async with LifespanManager(create_app(settings)) as manager:
      application=manager.app
      async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application),base_url='http://test') as client:
        async def call(method,path,headers=None,body=None,expected=200,params=None):
            response=await client.request(method,'/api'+path,headers=headers,json=body,params=params)
            if response.status_code not in (expected if isinstance(expected,tuple) else (expected,)):
                raise RuntimeError(f'{action}: {method} {path.split("?")[0]} status {response.status_code}, expected {expected}')
            if isinstance(expected,tuple):return {'status':response.status_code,'code':response.json().get('code')}
            return response.json()
        async def login(username):
            b=await call('POST','/auth/login',body={'username':username,'password':state['password']})
            return {'Authorization':'Bearer '+b['accessToken']}
        async def register(username):
            await call('POST','/auth/register',body={'username':username,'password':state['password']},expected=201)
            return await login(username)
        async def survey(headers,definition,sources=None):
            row=await call('POST','/surveys',headers,{'title':definition['title']},201)
            row=await call('PATCH',f'/surveys/{row["id"]}/settings',headers,{'expectedSettingsRevision':0,'allowMultiple':True})
            row=await save(headers,row,definition,sources)
            version=await publish(headers,row)
            row=await call('GET',f'/surveys/{row["id"]}',headers)
            return row,version
        async def save(headers,row,definition,sources=None):
            body={'expectedDraftRevision':row['draftRevision'],'definition':definition}
            if sources is not None:body['questionSources']=sources
            return await call('PUT',f'/surveys/{row["id"]}/draft',headers,body)
        async def publish(headers,row):
            return await call('POST',f'/surveys/{row["id"]}/publish',headers,{
                'expectedDraftRevision':row['draftRevision'],'expectedCurrentVersionId':row['currentVersionId'],'publishKey':str(uuid4())},201)
        async def start(headers,row):return await call('POST',f'/survey/{row["publicId"]}/start',headers)
        async def submit(headers,row,attempt,answers,anonymous=True,key=None,expected=201):
            return await call('POST',f'/survey/{row["publicId"]}/submit',{
                **headers,'X-Fill-Token':attempt['fillToken'],'Idempotency-Key':key or str(uuid4())},
                {'versionId':attempt['versionId'],'anonymous':anonymous,'answers':answers},expected)
        async def stats(headers,row,version):
            return await call('GET',f'/surveys/{row["id"]}/stats',headers,params={'versionId':version['id']})
        def number_definition(title):
            return {'title':title,'description':'Synthetic rehearsal only','allowAnonymous':True,'entryQuestionId':'score',
                'questions':[{'id':'score','kind':'number','title':'Score','required':True,'config':{'min':0,'max':10,'integerOnly':True},'defaultTarget':'END','rules':[]}]}
        if action=='seed-old':
            headers=await register('rehearsal_owner');definition=number_definition('Legacy survey')
            # Four types, simple ordered path, real validation and persistence.
            definition['entryQuestionId']='single'
            definition['questions']=[
              {'id':'single','kind':'single','title':'Single','required':True,'config':{'options':[{'id':'a','label':'A'},{'id':'b','label':'B'}]},'defaultTarget':'multi','rules':[]},
              {'id':'multi','kind':'multi','title':'Multi','required':True,'config':{'options':[{'id':'a','label':'A'},{'id':'b','label':'B'}],'minSelected':1,'maxSelected':2},'defaultTarget':'text','rules':[]},
              {'id':'text','kind':'text','title':'Text','required':True,'config':{'minLength':1,'maxLength':100},'defaultTarget':'score','rules':[]},*definition['questions']]
            legacy,v1=await survey(headers,definition)
            values={'single':'a','multi':['a','b'],'text':'synthetic answer','score':0}
            key=str(uuid4());receipt=await submit(headers,legacy,await start(headers,legacy),values,key=key)
            values3={**values,'score':3}
            await submit(headers,legacy,await start(headers,legacy),values3,anonymous=False)
            pending=await start(headers,legacy)
            definition2=deepcopy(definition);definition2['questions'][-1]['config']['max']=5
            saved=await save(headers,legacy,definition2);v2=await publish(headers,saved)
            legacy=await call('GET',f'/surveys/{legacy["id"]}',headers)
            state.update(legacy=legacy,v1=v1,v2=v2,answers=values,replayKey=key,replayReceipt=receipt,pending=pending)
            assert (await stats(headers,legacy,v1))['totalResponses']==2
            checks.update(legacyFourTypes=True,legacyVersions=2,legacyResponses=2,anonymousAndNamed=True,oldFillCredentialCaptured=True)
        elif action=='upgrade-current':
            # Initialization has completed; compare before any auth/feature write.
            initial=read(output/'seed-backup/manifest.json')
            assert all(document_hashes(db,OLD)[n]==initial[n]['documentsSha256'] for n in OLD),'Old records changed during initialization'
            from bson import decode_all
            for name in OLD:
                oldmeta=decode_all((output/'seed-backup'/(name+'.meta.bson')).read_bytes())[0]
                current=next(db.list_collections(filter={'name':name}))['options']
                normalized=deepcopy(current)
                optional={'surveys':'draftQuestionSources','survey_versions':'questionSources'}.get(name)
                if optional:normalized['validator']['$jsonSchema']['properties'].pop(optional,None)
                assert normalized==oldmeta['options'],'Unexpected legacy contract change'
                current_ix={i['name']:index_contract(i) for i in db[name].list_indexes()}
                assert all(current_ix[i['name']]==index_contract(i) for i in oldmeta['indexes'])
            headers=await login('rehearsal_owner');legacy=state['legacy']
            assert (await stats(headers,legacy,state['v1']))['totalResponses']==2
            replay=await submit(headers,legacy,state['pending'],state['answers'],key=state['replayKey'],expected=200)
            assert replay==state['replayReceipt']
            delayed=await submit(headers,legacy,state['pending'],{**state['answers'],'score':8})
            assert delayed['versionId']==state['v1']['id']
            assert (await stats(headers,legacy,state['v1']))['totalResponses']==3
            legacy_ids={name:[r['_id'] for r in db[name].find({})] for name in OLD}
            legacy_hash={name:fingerprint(list(db[name].find({'_id':{'$in':ids}}).sort('_id',1))) for name,ids in legacy_ids.items()}
            content={'kind':'number','title':'Shared rehearsal score','config':{'min':0,'max':10,'integerOnly':True}}
            qv1=await call('POST','/questions',{**headers,'Idempotency-Key':str(uuid4())},{'content':content},201)
            receiver=await register('rehearsal_receiver')
            await call('PUT',f'/questions/{qv1["questionId"]}/shares',headers,{'recipientUsername':'rehearsal_receiver','active':True,'expectedRevision':0},201)
            def sourced(title):
                value=number_definition(title);value['questions'][0].update(deepcopy(content));return value
            mapping=[{'instanceId':'score','questionId':qv1['questionId'],'questionVersionId':qv1['id']}]
            a,av=await survey(headers,sourced('Source A'),mapping)
            b,bv=await survey(receiver,sourced('Source B'),mapping)
            await submit(headers,a,await start(headers,a),{'score':0})
            await submit(receiver,b,await start(receiver,b),{'score':3})
            bank=await call('POST','/question-banks',{**headers,'Idempotency-Key':str(uuid4())},{'name':'Rehearsal bank','description':'Synthetic only'},201)
            await call('PUT',f'/question-banks/{bank["id"]}/questions/{qv1["questionId"]}',headers,{'questionVersionId':qv1['id'],'expectedRevision':0})
            qv2=await call('POST',f'/questions/{qv1["questionId"]}/versions',{**headers,'Idempotency-Key':str(uuid4())},{'parentVersionId':qv1['id'],'expectedCurrentVersionId':qv1['id'],'content':{**content,'title':'Revised rehearsal score'}},201)
            qv3=await call('POST',f'/questions/{qv1["questionId"]}/restore',{**headers,'Idempotency-Key':str(uuid4())},{'targetVersionId':qv1['id'],'expectedCurrentVersionId':qv2['id']},201)
            assert qv3['content']==qv1['content'] and qv3['versionNo']==3
            await call('PUT',f'/surveys/{b["id"]}/question-statistics-grants/{qv1["questionId"]}',receiver,{'active':True,'expectedRevision':0})
            boundary=await call('POST',f'/questions/{qv1["questionId"]}/statistics/scopes',{**headers,'Idempotency-Key':str(uuid4())},{},201)
            summary=await call('GET',f'/questions/{qv1["questionId"]}/statistics',headers,params={'scopeId':boundary['id']})
            assert summary['versions'][0]['sum']==3 and summary['versions'][0]['A']==2
            raw=await call('GET',f'/questions/{qv1["questionId"]}/statistics/answers',headers,params={'scopeId':boundary['id']})
            assert [r['value'] for r in raw['items']]==[0]
            assert (await stats(headers,a,av))['totalResponses']==1
            assert (await stats(receiver,b,bv))['totalResponses']==1
            assert all(fingerprint(list(db[n].find({'_id':{'$in':ids}}).sort('_id',1)))==legacy_hash[n] for n,ids in legacy_ids.items())
            state.update(sourceSurvey=a,sourceVersion=av,question=qv1,bank=bank)
            checks.update(oldInitializationDocumentsUnchanged=True,legacyContractsOnlyGainOptionalSources=True,oldIndexesPreserved=True,
                oldLoginAndStats=True,oldSubmissionReplay=True,oldCredentialAfterNewerPublication=True,
                phase2IndependentVersionsAndRestore=True,phase2FixedSources=True,phase2SharedUse=True,phase2BankPin=True,
                aggregateGrantAndScope=True,aggregateA=2,aggregateSum=3,ownRawOnly=True,legacyRecordsProtectedDuringNewFeatureWrites=True)
        elif action=='mixed-old':
            assert document_hashes(db,OLD+NEW)==before
            headers=await login('rehearsal_owner');row=state['sourceSurvey']
            assert (await stats(headers,state['legacy'],state['v1']))['totalResponses']==3
            current=await call('GET',f'/surveys/{row["id"]}',headers)
            attempt=await start(headers,row)
            changed=deepcopy(current['draft']);changed['questions'][0]['title']='Old app mutation attempt'
            save_result=await call('PUT',f'/surveys/{row["id"]}/draft',headers,{'expectedDraftRevision':current['draftRevision'],'definition':changed},(422,503))
            assert save_result['code'] in ('INVALID_DOCUMENT','DATABASE_UNAVAILABLE')
            publish_result=await call('POST',f'/surveys/{row["id"]}/publish',headers,{'expectedDraftRevision':current['draftRevision'],'expectedCurrentVersionId':current['currentVersionId'],'publishKey':str(uuid4())},(422,503))
            assert publish_result['code'] in ('INVALID_DOCUMENT','DATABASE_UNAVAILABLE')
            submit_result=await submit(headers,row,attempt,{'score':4},expected=(422,503))
            assert submit_result['code'] in ('INVALID_DOCUMENT','DATABASE_UNAVAILABLE','SUBMISSION_OUTCOME_UNKNOWN')
            from pymongo.errors import WriteError
            try:
                db.surveys.update_one({'_id':row['id']},{'$inc':{'draftRevision':1}})
            except WriteError as error:
                assert error.code==121
            else:
                raise RuntimeError('Old strict validator unexpectedly accepted the sourced record')
            assert document_hashes(db,OLD+NEW)==before,'Rejected old writes changed documents'
            properties=next(db.list_collections(filter={'name':'surveys'}))['options']['validator']['$jsonSchema']['properties']
            assert 'draftQuestionSources' not in properties
            checks.update(oldLegacyRead=True,oldStartupReplacedValidators=True,sourceSaveStatus=save_result['status'],sourcePublishStatus=publish_result['status'],sourceSubmitStatus=submit_result['status'],
                          errorCodes=[save_result['code'],publish_result['code'],submit_result['code']],databaseValidationError121Confirmed=True,allBusinessDocumentsUnchanged=True,unsafeDirectDowngradeRejected=True)
        elif action=='repair-current':
            assert document_hashes(db,OLD+NEW)==before
            headers=await login('rehearsal_owner');row=await call('GET',f'/surveys/{state["sourceSurvey"]["id"]}',headers)
            assert row['draftQuestionSources'][0]['questionVersionId']==state['question']['id']
            assert (await call('GET',f'/questions/{state["question"]["questionId"]}',headers))['lastVersionNo']==3
            assert document_hashes(db,OLD+NEW)==before
            checks.update(currentValidatorsRestored=True,fixedSourcesReadable=True,independentHistoryReadable=True,allBusinessDocumentsUnchanged=True)
        elif action=='restored-old':
            initial=read(output/'seed-backup/manifest.json')
            assert all(document_hashes(db,OLD)[n]==initial[n]['documentsSha256'] for n in OLD)
            from bson import decode_all
            for n in OLD:
                meta=decode_all((output/'seed-backup'/(n+'.meta.bson')).read_bytes())[0]
                assert next(db.list_collections(filter={'name':n}))['options']==meta['options']
                assert {i['name']:index_contract(i) for i in db[n].list_indexes()}=={i['name']:index_contract(i) for i in meta['indexes']}
            headers=await login('rehearsal_owner');row=await call('GET',f'/surveys/{state["legacy"]["id"]}',headers)
            assert (await stats(headers,row,state['v1']))['totalResponses']==2
            value=deepcopy(row['draft']);value['title']='Restored legacy writable'
            saved=await save(headers,row,value);assert saved['draftRevision']==row['draftRevision']+1
            v=await publish(headers,saved)
            row=await call('GET',f'/surveys/{row["id"]}',headers)
            await submit(headers,row,await start(headers,row),{**state['answers'],'score':2})
            assert (await stats(headers,row,v))['totalResponses']==1
            checks.update(restorePointDocumentsExact=True,restoreContractsAndIndexesExact=True,oldLoginReadWritePublishSubmit=True,
                          laterPhase2WritesNotInRestoredBackup=True,notLosslessDowngrade=True)
        else:raise ValueError('Unknown worker action')
    dump(state_path,state);dump(output/(action+'.json'),checks);control.close()

def run(uri,output,git_executable):
    from pymongo import MongoClient
    validate_uri(uri);output=validate_output(output);output.mkdir(parents=True)
    run_id=uuid4().hex
    databases={stage:'survey_upgrade_'+stage+'_'+uuid4().hex for stage in ('seed','upgraded','mixed','restored')}
    ownership={'runId':run_id,'databases':databases}
    dump(output/'ownership.private.json',ownership)
    client=MongoClient(uri,serverSelectionTimeoutMS=5000)
    owned=[];results={'passed':False,'baselineCommit':BASELINE,'mode':'real two-version ASGI lifecycle; isolated BSON clone rehearsal','realDataMigrated':False,'dependencyReinstallationTested':False,'operatingSystemRollbackTested':False,'losslessDowngradeClaimed':False}
    current=output/'current-code';baseline=output/'baseline-code';current.mkdir();baseline.mkdir()
    env={**os.environ,'MONGO_URI':uri,'MONGO_DATABASE':databases['seed']}
    for name in ('AUTH_SECRET','FILL_SECRET','PARTICIPANT_SECRET'):env[name]=secrets.token_urlsafe(48)
    def git(*args):return subprocess.run([git_executable,'-C',str(ROOT),*args],capture_output=True,check=True)
    assert git('rev-parse',BASELINE).stdout.decode().strip()==BASELINE
    archive=git('archive','--format=zip',BASELINE,'backend','requirements.txt').stdout
    with zipfile.ZipFile(io.BytesIO(archive)) as package:
        archive_members(package.namelist());package.extractall(baseline)
    code_hashes={}
    for source in (ROOT/'backend').rglob('*.py'):
        assert not source.is_symlink()
        relative=source.relative_to(ROOT);target=current/relative;target.parent.mkdir(parents=True,exist_ok=True)
        target.write_bytes(source.read_bytes());code_hashes[relative.as_posix()]=hashlib.sha256(target.read_bytes()).hexdigest()
    assert (ROOT/'requirements.txt').read_bytes()==(baseline/'requirements.txt').read_bytes()
    results.update(currentBaseCommit=git('rev-parse','HEAD').stdout.decode().strip(),currentBackendWorkingTreeHash=fingerprint(code_hashes),
        currentBackendHasUncommittedChanges=bool(git('diff','--name-only','HEAD','--','backend').stdout.strip()),sameLockedRequirements=True)
    def create(stage):
        name=validate_database(databases[stage])
        if name in client.list_database_names():raise RuntimeError('Generated rehearsal database already exists')
        client[name][MARKER].insert_one({'_id':'owner','runId':run_id,'stage':stage});owned.append(stage)
    def step(action,code,stage):
        env['MONGO_DATABASE']=databases[stage]
        command=[sys.executable,str(Path(__file__).resolve()),'__worker',action,str(code),databases[stage],str(output)]
        results['lastStepAttempted']=action
        response=subprocess.run(command,env=env,capture_output=True,text=True,encoding='utf-8',timeout=180)
        # No HTTP payloads or environment secrets are copied to public evidence.
        (output/(action+'.stderr.private.txt')).write_text(response.stderr,encoding='utf-8')
        if response.returncode:raise RuntimeError('Worker failed: '+action+'; see private diagnostic')
        results[action]=read(output/(action+'.json'))
        print(json.dumps({'rehearsalStep':action,'passed':True}),flush=True)
    try:
        create('seed');step('seed-old',baseline,'seed')
        seed=export_backup(client,databases['seed'],output/'seed-backup',OLD)
        create('upgraded');restore_backup(client,databases['upgraded'],output/'seed-backup',OLD)
        step('upgrade-current',current,'upgraded')
        upgraded=export_backup(client,databases['upgraded'],output/'upgraded-backup',OLD+NEW)
        create('mixed');restore_backup(client,databases['mixed'],output/'upgraded-backup',OLD+NEW)
        step('mixed-old',baseline,'mixed');step('repair-current',current,'mixed')
        create('restored');restore_backup(client,databases['restored'],output/'seed-backup',OLD)
        step('restored-old',baseline,'restored')
        assert document_hashes(client[databases['upgraded']],OLD+NEW)=={n:v['documentsSha256'] for n,v in upgraded.items()}
        results.update(passed=True,seedBackup=seed,upgradedBackup=upgraded,upgradedDatabaseRetainedDuringBackupRollback=True)
    finally:
        cleanup=[]
        for stage in owned:
            name=databases[stage];check_marker(client[name][MARKER].find_one({'_id':'owner'}),run_id,stage)
            client.drop_database(name);absent=name not in client.list_database_names()
            cleanup.append({'stage':stage,'ownershipVerified':True,'databaseAbsent':absent})
        results['cleanup']=cleanup;dump(output/'results.json',results);client.close()
    return results

def main():
    if len(sys.argv)>1 and sys.argv[1]=='__worker':
        _,_,action,code,database,output=sys.argv
        asyncio.run(worker(action,Path(code),database,Path(output)));return
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--mongo-uri',required=True);p.add_argument('--output',type=Path,required=True);p.add_argument('--git',default='git')
    args=p.parse_args();results=run(args.mongo_uri,args.output,args.git)
    print(json.dumps({'passed':results['passed'],'ownedDatabasesCleaned':len(results['cleanup']),'rawBackupsPrivate':True}))

if __name__=='__main__':main()
