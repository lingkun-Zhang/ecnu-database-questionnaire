<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import AnswerInput from './components/AnswerInput.vue'
import DefinitionEditor from './components/DefinitionEditor.vue'
import QuestionLibrary from './components/QuestionLibrary.vue'
import { ApiError, download, getToken, post, request, setToken } from './api'
import { clone, exampleDefinition, kindNames, uid } from './types'
import { resolveEditorError } from './editorErrors'
import { applyQuestionVersion, detachQuestionSource, sourcesInDefinition } from './questionReuse'
import type { Answer, Answers, Definition, DesignWarning, Evaluation, FieldError, QuestionSource, QuestionVersion, Receipt, RespondentType, ResponseItem, Stats, Survey, User, Version } from './types'

const publicId = decodeURIComponent(location.pathname.match(/^\/survey\/([^/]+)/)?.[1] || '')
const user=ref<User|null>(null), booting=ref(true), busy=ref(false), error=ref<ApiError|null>(null), notice=ref('')
const authMode=ref<'login'|'register'>('login'), username=ref(''), password=ref('')
const page=ref<'dashboard'|'editor'|'statistics'|'questions'>('dashboard')
const questionLibrary=ref<{canLeave:()=>boolean;hasUnsavedChanges:()=>boolean}|null>(null),questionBusy=ref(false)
const localQuestionSources=ref<QuestionSource[]|null>(null),questionPicker=ref<{surveyId:string;generation:number;instanceId?:string}|null>(null)
const currentQuestionSources=computed(()=>localQuestionSources.value??active.value?.draftQuestionSources??[])
const pickerSource=computed(()=>currentQuestionSources.value.find(source=>source.instanceId===questionPicker.value?.instanceId))
const surveys=ref<Survey[]>([]), nextSurveyCursor=ref<string|null>(null), search=ref(''), statusFilter=ref('all')
const active=ref<Survey|null>(null), definition=ref<Definition|null>(null), savedDefinition=ref('')
const remoteDraftChanged=ref(false)
const editorTab=ref<'design'|'preview'|'settings'>('design'), versions=ref<Version[]>([])
const deadlineInput=ref(''), allowMultipleInput=ref(false)
const previewAnswers=ref<Answers>({}), previewResult=ref<Evaluation|null>(null), previewBusy=ref(false)
const showHistory=ref(false), leftVersionId=ref(''), rightVersionId=ref(''), showHelp=ref(false)
const closeConfirmation=ref<{id:string;title:string;revision:number}|null>(null)
const stats=ref<Stats|null>(null), statsVersion=ref(''), statsQuestion=ref(''), detailItems=ref<ResponseItem[]>([]), nextDetailCursor=ref<string|null>(null)
const statsRespondentType=ref<RespondentType>('all')
const respondentTypeNames:Record<RespondentType,string>={all:'全部填写方式',anonymous:'仅匿名填写',named:'仅实名填写'}
const publicInfo=ref<Record<string,unknown>|null>(null), fillDefinition=ref<Definition|null>(null), fillToken=ref(''), fillVersionId=ref(''), fillExpiresAt=ref('')
const fillVersionNo=ref<number|null>(null)
const fillAnswers=ref<Answers>({}), fillResult=ref<Evaluation|null>(null), fillCurrentId=ref(''), fillBusy=ref(false), touched=ref(new Set<string>()), attemptedNext=ref(false)
const receipt=ref<Receipt|null>(null)
interface PendingSubmission { key: string; body: {versionId:string;anonymous:boolean;answers:Answers}; fillToken:string }
const pending=ref<PendingSubmission|null>(null), anonymous=ref(false)
let evaluateCounter=0, fillTimer:ReturnType<typeof setTimeout>|undefined, previewTimer:ReturnType<typeof setTimeout>|undefined, previewCounter=0
let pendingPublish:{publishKey:string;expectedDraftRevision:number;expectedCurrentVersionId:string|null}|null=null
let sessionGeneration=0
let statsCounter=0, detailCounter=0
let statsBoundary:{surveyId:string;versionId:string;asOfSeq:number}|null=null
let definitionCounter=0
interface SessionScope { generation:number; token:string }
interface DefinitionCheck { session:SessionScope; surveyId:string; snapshot:string; generation:number }
const definitionCheck=ref<DefinitionCheck|null>(null)
const designCheck=ref<DefinitionCheck|null>(null), designWarnings=ref<DesignWarning[]|null>(null)
const designCheckNote=ref('')
const definitionEditor=ref<{focusError:(issue:FieldError)=>Promise<boolean>}|null>(null)
class StaleSessionError extends Error {}
function captureSession():SessionScope{return {generation:sessionGeneration,token:getToken()}}
function isCurrentSession(scope:SessionScope){return scope.generation===sessionGeneration&&scope.token===getToken()}
function assertSession(scope:SessionScope){if(!isCurrentSession(scope))throw new StaleSessionError()}
function captureDefinitionCheck():DefinitionCheck|null{return active.value&&definition.value?{session:captureSession(),surveyId:active.value.id,snapshot:JSON.stringify(definition.value),generation:definitionCounter}:null}
function isCurrentDefinitionCheck(check:DefinitionCheck){return isCurrentSession(check.session)&&page.value==='editor'&&active.value?.id===check.surveyId&&definitionCounter===check.generation&&JSON.stringify(definition.value)===check.snapshot}
function isDefinitionError(code:string){return code==='INVALID_DEFINITION'||code==='INVALID_QUESTION_SOURCE'}
const editorFieldErrors=computed(()=>error.value&&isDefinitionError(error.value.code)&&definitionCheck.value&&isCurrentDefinitionCheck(definitionCheck.value)?error.value.fieldErrors:[])
const currentDesignWarnings=computed(()=>designCheck.value&&isCurrentDefinitionCheck(designCheck.value)?designWarnings.value:null)
function relatedRulesLabel(issue:DesignWarning){
  const rules=definition.value?.questions.find(q=>q.id===issue.questionId)?.rules||[]
  return (issue.relatedRuleIds||[]).map(id=>{const index=rules.findIndex(rule=>rule.id===id);return index<0?'':`条件规则 ${index+1}（优先级 ${rules[index]!.priority}）`}).filter(Boolean).join('、')
}
function editorErrorLabel(issue:FieldError){return definition.value?resolveEditorError(definition.value,issue).label:'查看错误'}
async function focusEditorIssue(issue:FieldError){
  const check=definitionCheck.value
  if(!check||!isCurrentDefinitionCheck(check)||!editorFieldErrors.value.includes(issue))return
  editorTab.value='design';await nextTick()
  if(!isCurrentDefinitionCheck(check)||!editorFieldErrors.value.includes(issue))return
  await definitionEditor.value?.focusError(issue)
}
async function focusDesignWarning(issue:DesignWarning){
  const check=designCheck.value
  if(!check||!isCurrentDefinitionCheck(check)||!currentDesignWarnings.value?.includes(issue))return
  editorTab.value='design';await nextTick()
  if(!isCurrentDefinitionCheck(check)||!currentDesignWarnings.value?.includes(issue))return
  await definitionEditor.value?.focusError(issue)
}

const dirty=computed(()=>definition.value!=null && (JSON.stringify(definition.value)!==savedDefinition.value||JSON.stringify(currentQuestionSources.value)!==JSON.stringify(active.value?.draftQuestionSources??[])))
const filteredSurveys=computed(()=>surveys.value.filter(s=>(statusFilter.value==='all'||statusOf(s)===statusFilter.value)&&`${s.draft.title} ${s.draft.description}`.toLowerCase().includes(search.value.toLowerCase())))
const openCount=computed(()=>surveys.value.filter(s=>statusOf(s)==='open').length)
const selectedVersion=computed(()=>versions.value.find(v=>v.id===statsVersion.value))
const statisticsTitle=computed(()=>selectedVersion.value?.definition.title || active.value?.draft.title || '问卷')
const displayedQuestions=computed(()=>stats.value?.questions.filter(q=>!statsQuestion.value || q.questionId===statsQuestion.value)||[])
const historyLeft=computed(()=>versions.value.find(v=>v.id===leftVersionId.value)), historyRight=computed(()=>versions.value.find(v=>v.id===rightVersionId.value))
const differences=computed(()=>{
  const left=historyLeft.value?.definition,right=historyRight.value?.definition;if(!left||!right)return []
  const result:Array<{kind:string;title:string;before?:unknown;after?:unknown}>=[]
  for(const key of ['title','description','allowAnonymous','entryQuestionId'] as const)if(JSON.stringify(left[key])!==JSON.stringify(right[key]))result.push({kind:'信息变化',title:({title:'问卷标题',description:'问卷说明',allowAnonymous:'匿名政策',entryQuestionId:'入口题'})[key],before:left[key],after:right[key]})
  for(const q of left.questions){const next=right.questions.find(x=>x.id===q.id);if(!next)result.push({kind:'移除题目',title:q.title,before:q});else if(JSON.stringify(q)!==JSON.stringify(next))result.push({kind:'修改题目',title:next.title,before:q,after:next})}
  for(const q of right.questions)if(!left.questions.some(x=>x.id===q.id))result.push({kind:'新增题目',title:q.title,after:q})
  if(left.questions.map(q=>q.id).join()!==right.questions.map(q=>q.id).join())result.push({kind:'顺序变化',title:'题目显示顺序',before:left.questions.map(q=>q.title),after:right.questions.map(q=>q.title)})
  return result
})
const currentQuestion=computed(()=>fillDefinition.value?.questions.find(q=>q.id===fillCurrentId.value))
const fillPath=computed(()=>fillResult.value?.path || (fillDefinition.value?.entryQuestionId ? [fillDefinition.value.entryQuestionId] : []))
const currentIndex=computed(()=>Math.max(0,fillPath.value.indexOf(fillCurrentId.value)))
const currentErrors=computed(()=>fillResult.value?.errors.filter(e=>e.questionId===fillCurrentId.value && (touched.value.has(fillCurrentId.value)||attemptedNext.value))||[])
const fillTitle=computed(()=>fillDefinition.value?.title || String((publicInfo.value?.definition as Definition|undefined)?.title || publicInfo.value?.title || '问卷邀请'))
const availabilityMessage=computed(()=>publicInfo.value?.status==='closed'?'这份问卷已经关闭，暂不接受新的答卷。':publicInfo.value?.status==='expired'?'这份问卷已经超过截止时间。':'')
const friendlyCodes:Record<string,string>={DRAFT_CONFLICT:'草稿已在其他页面更新。请重新载入并比较后再保存。',PUBLISH_CONFLICT:'发布依据已变化，请刷新草稿并重新确认。',POLICY_LOCKED:'已经收到正式答卷，多次填写政策已锁定。',ALREADY_SUBMITTED:'你已经填写过这份问卷，当前设置不允许重复填写。',SURVEY_CLOSED:'这份问卷已经关闭，暂不接受新的答卷。',SURVEY_EXPIRED:'这份问卷已经超过截止时间。',IDEMPOTENCY_CONFLICT:'同一提交标识的内容发生变化。请恢复原内容重试，或开始新的填写。',FILL_CREDENTIAL_EXPIRED:'本次填写凭据已过期。尚未提交的答卷需要重新开始。'}

function date(value:string|null|undefined){if(!value)return '未设置';return new Intl.DateTimeFormat('zh-CN',{year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'}).format(new Date(value))}
function statusOf(s:Survey){return s.status==='open'&&s.deadlineAt&&Date.parse(s.deadlineAt)<=Date.now()?'expired':s.status}
function statusName(s:Survey){return ({open:'收集中',draft:'未发布',closed:'已关闭',expired:'已截止'} as Record<string,string>)[statusOf(s)]||s.status}
function clearAccountViews(){
  sessionGeneration++;evaluateCounter++;previewCounter++;statsCounter++;detailCounter++;definitionCounter++
  clearTimeout(fillTimer);clearTimeout(previewTimer);fillTimer=undefined;previewTimer=undefined
  user.value=null;page.value='dashboard';surveys.value=[];nextSurveyCursor.value=null;search.value='';statusFilter.value='all'
  active.value=null;definition.value=null;definitionCheck.value=null;designCheck.value=null;designWarnings.value=null;designCheckNote.value='';savedDefinition.value='';editorTab.value='design';versions.value=[];pendingPublish=null
  localQuestionSources.value=null;questionPicker.value=null;questionBusy.value=false
  remoteDraftChanged.value=false
  deadlineInput.value='';allowMultipleInput.value=false;previewAnswers.value={};previewResult.value=null;previewBusy.value=false
  showHistory.value=false;leftVersionId.value='';rightVersionId.value='';showHelp.value=false;closeConfirmation.value=null
  stats.value=null;statsVersion.value='';statsQuestion.value='';statsRespondentType.value='all';statsBoundary=null;detailItems.value=[];nextDetailCursor.value=null
  publicInfo.value=null;fillDefinition.value=null;fillToken.value='';fillVersionId.value='';fillVersionNo.value=null;fillExpiresAt.value=''
  fillAnswers.value={};fillResult.value=null;fillCurrentId.value='';fillBusy.value=false;touched.value=new Set();attemptedNext.value=false
  receipt.value=null;pending.value=null;anonymous.value=false;busy.value=false;error.value=null;notice.value='';password.value='';username.value='';authMode.value='login';booting.value=false
  // Keep each account's sessionStorage recovery record; a new account restores only its own key.
}
function showError(value:unknown,scope=captureSession(),check=captureDefinitionCheck()){
  if(value instanceof StaleSessionError||!isCurrentSession(scope))return
  const failure=value instanceof ApiError?value:new ApiError(0,'CLIENT_ERROR',value instanceof Error?value.message:'操作未完成，请重试。')
  if(isDefinitionError(failure.code)&&check&&!isCurrentDefinitionCheck(check))return
  if(failure.status===401){setToken('');clearAccountViews()}
  if(publicId&&!fillDefinition.value&&(failure.code==='SURVEY_CLOSED'||failure.code==='SURVEY_EXPIRED'))publicInfo.value={...publicInfo.value,status:failure.code==='SURVEY_CLOSED'?'closed':'expired'}
  definitionCheck.value=isDefinitionError(failure.code)?check:null
  error.value=failure
}
async function perform(action:()=>Promise<void>,success?:string){const scope=captureSession(),check=captureDefinitionCheck();busy.value=true;error.value=null;definitionCheck.value=null;notice.value='';try{await action();assertSession(scope);if(success)notice.value=success}catch(value){showError(value,scope,check)}finally{if(isCurrentSession(scope))busy.value=false}}
function clearFeedback(){error.value=null;notice.value=''}
function localTime(value:string|null){if(!value)return '';const d=new Date(value);return new Date(d.getTime()-d.getTimezoneOffset()*60000).toISOString().slice(0,16)}
function pendingStorageKey(){return `wenchuan_pending_${publicId}_${user.value?.id||''}`}
function rememberPending(){if(pending.value)sessionStorage.setItem(pendingStorageKey(),JSON.stringify(pending.value));else sessionStorage.removeItem(pendingStorageKey())}
function restorePending(){try{pending.value=JSON.parse(sessionStorage.getItem(pendingStorageKey())||'null')}catch{pending.value=null}}
function questionTitle(id:string,def:Definition|null=definition.value){return id==='END'?'结束':def?.questions.find(q=>q.id===id)?.title||id}
function shareURL(s:Survey){return `${location.origin}/survey/${encodeURIComponent(s.publicId)}`}
async function copyShare(s:Survey){const scope=captureSession();try{await navigator.clipboard.writeText(shareURL(s));assertSession(scope);notice.value='分享链接已复制。填写者登录后即可访问。'}catch{if(isCurrentSession(scope))notice.value=`分享链接：${shareURL(s)}`}}
function traceText(item:Record<string,unknown>){return String(item.reason || item.message || item.description || (item.ruleId ? `命中规则 ${item.ruleId}` : item.target ? `采用默认去向 → ${questionTitle(String(item.target))}` : JSON.stringify(item)))}

async function loadSurveys(append=false){const scope=captureSession(),offset=append?surveys.value.length:0;const result=await request<{items:Survey[]}>(`/surveys?limit=50&offset=${offset}`);assertSession(scope);surveys.value=append?[...surveys.value,...result.items]:result.items;nextSurveyCursor.value=result.items.length===50?String(offset+50):null}
async function loadVersions(){if(!active.value)return;const scope=captureSession(),id=active.value.id,items:Version[]=[];let offset=0;while(true){const result=await request<{items:Version[]}>(`/surveys/${id}/versions?limit=100&offset=${offset}`);assertSession(scope);items.push(...result.items);if(result.items.length<100)break;offset+=100}versions.value=items}
async function loadActive(id:string){const scope=captureSession();const value=await request<Survey>(`/surveys/${id}`);assertSession(scope);active.value=value;definition.value=clone(value.draft);localQuestionSources.value=null;remoteDraftChanged.value=false;savedDefinition.value=JSON.stringify(definition.value);deadlineInput.value=localTime(value.deadlineAt);allowMultipleInput.value=value.allowMultiple;pendingPublish=null;await loadVersions();assertSession(scope)}
async function authenticate(){
  let scope=captureSession();const credentials={username:username.value,password:password.value},mode=authMode.value
  busy.value=true;clearFeedback()
  try{
    let result:{accessToken?:string;user?:User}={}
    if(mode==='register'){result=await post('/auth/register',credentials);assertSession(scope)}
    if(!result.accessToken){result=await post('/auth/login',credentials);assertSession(scope)}
    if(!result.accessToken)throw new Error('登录接口未返回有效令牌。')
    clearAccountViews();setToken(result.accessToken);scope=captureSession();busy.value=true
    const account=result.user||await request<User>('/auth/me');assertSession(scope);user.value=account
    if(publicId){restorePending();await loadPublic()}else await loadSurveys()
    assertSession(scope)
  }catch(value){showError(value,scope)}finally{if(isCurrentSession(scope))busy.value=false}
}
function logout(){if(page.value==='questions'&&questionLibrary.value?.hasUnsavedChanges()&&!confirm('题目还有未保存的编辑或结果未确认的操作，确定退出？已保存结果可在重新登录后查看。'))return;setToken('');clearAccountViews()}
function expireQuestionSession(){setToken('');clearAccountViews();error.value=new ApiError(401,'UNAUTHENTICATED','登录已过期，请重新登录。')}
function canLeaveQuestions(){return !questionBusy.value&&(page.value!=='questions'||questionLibrary.value?.canLeave()!==false)}
async function goDashboard(){if(busy.value||questionBusy.value||questionPicker.value||!canLeaveQuestions())return;if(dirty.value&&!confirm('还有未保存的草稿，确定离开？'))return;page.value='dashboard';active.value=null;definition.value=null;localQuestionSources.value=null;questionPicker.value=null;clearFeedback();await perform(()=>loadSurveys())}
function openQuestions(){if(busy.value||questionBusy.value||questionPicker.value)return;if(page.value==='questions')return;if(dirty.value&&!confirm('还有未保存的草稿，确定离开？'))return;active.value=null;definition.value=null;localQuestionSources.value=null;questionPicker.value=null;showHistory.value=false;clearFeedback();page.value='questions'}
function openQuestionPicker(instanceId?:string){if(busy.value||questionBusy.value||!active.value||!definition.value||page.value!=='editor')return;questionPicker.value={surveyId:active.value.id,generation:sessionGeneration,...(instanceId===undefined?{}:{instanceId})}}
function closeQuestionPicker(){if(questionBusy.value)return;questionPicker.value=null}
function chooseQuestionVersion(version:QuestionVersion){
  if(busy.value||questionBusy.value||!questionPicker.value||!active.value||!definition.value||page.value!=='editor'||questionPicker.value.surveyId!==active.value.id||questionPicker.value.generation!==sessionGeneration)return
  try{
    const instanceId=questionPicker.value.instanceId
    if(instanceId!==undefined&&!confirm(`把这道题更换为「${version.content.title}」V${version.versionNo}？必答和跳转会保留，请检查规则是否仍适用。`))return
    const result=applyQuestionVersion(definition.value,currentQuestionSources.value,version,instanceId)
    definition.value=result.definition;localQuestionSources.value=result.sources;questionPicker.value=null;clearFeedback()
    notice.value=instanceId===undefined?'题目已加入草稿，请检查并保存。':'题目版本已更换，请检查跳转规则后保存。'
    void runPreview()
  }catch(value){showError(value)}
}
function detachQuestion(instanceId:string){
  if(busy.value||questionBusy.value||!definition.value)return
  if(!confirm('解除个人题目引用？本卷保留当前题目内容、必答和跳转，之后可独立编辑。保存后生效。'))return
  localQuestionSources.value=detachQuestionSource(definition.value,currentQuestionSources.value,instanceId);clearFeedback();notice.value='引用已解除，请保存草稿。'
}
async function openSurvey(id:string,target:'editor'|'statistics'='editor'){if(busy.value||questionPicker.value||!canLeaveQuestions())return;await perform(async()=>{const scope=captureSession();await loadActive(id);assertSession(scope);page.value=target;editorTab.value='design';previewAnswers.value={};previewResult.value=null;if(target==='statistics'){statsVersion.value=active.value?.currentVersionId||versions.value[0]?.id||'';statsQuestion.value='';statsRespondentType.value='all';statsBoundary=null;await refreshStats()}})}
async function createSurvey(useExample=false){if(busy.value||questionBusy.value||questionPicker.value)return;await perform(async()=>{const scope=captureSession();const value=await post<Survey>('/surveys',{title:useExample?'学习与生活小调查':'未命名问卷'});assertSession(scope);await loadActive(value.id);assertSession(scope);page.value='editor';editorTab.value='design';if(useExample)definition.value=exampleDefinition()})}
async function saveDraft(){
  if(busy.value||questionBusy.value||questionPicker.value||!active.value||!definition.value)return
  const id=active.value.id
  await perform(async()=>{
    const scope=captureSession(),sources=currentQuestionSources.value
    const body={definition:definition.value,expectedDraftRevision:active.value!.draftRevision,
      ...(localQuestionSources.value!==null||active.value!.draftQuestionSources!==undefined?{questionSources:sourcesInDefinition(definition.value!,sources)}:{})}
    const result=await request<Survey>(`/surveys/${id}/draft`,{method:'PUT',body:JSON.stringify(body)})
    assertSession(scope);await loadActive(result.id||id)
  },'草稿已保存。发布前仍可继续编辑。')
}
async function publish(){if(busy.value||questionBusy.value||questionPicker.value||remoteDraftChanged.value||!active.value||dirty.value)return;const id=active.value.id;await perform(async()=>{const scope=captureSession();pendingPublish ||= {publishKey:uid('pub'),expectedDraftRevision:active.value!.draftRevision,expectedCurrentVersionId:active.value!.currentVersionId};await post(`/surveys/${id}/publish`,pendingPublish);assertSession(scope);await loadActive(id)},'新版本已发布。既有答卷始终保留原版本含义。')}
async function refreshMetadata(){
  if(!active.value)return
  const scope=captureSession(),id=active.value.id,value=await request<Survey>(`/surveys/${id}`);assertSession(scope)
  if(active.value?.id!==id)return
  if(value.draftRevision!==active.value.draftRevision)remoteDraftChanged.value=true
  // Collection settings must not silently accept another page's draft or sources.
  active.value={...active.value,status:value.status,deadlineAt:value.deadlineAt,allowMultiple:value.allowMultiple,policyLocked:value.policyLocked,settingsRevision:value.settingsRevision,currentVersionId:value.currentVersionId,lastVersionNo:value.lastVersionNo,updatedAt:value.updatedAt}
  deadlineInput.value=localTime(value.deadlineAt);allowMultipleInput.value=value.allowMultiple
}
async function reloadCurrent(){if(busy.value||questionBusy.value||questionPicker.value||!active.value||!confirm('重新载入会替换本页未保存的草稿。建议先在 JSON 高级编辑中复制本地内容用于对比。确定载入服务器草稿？'))return;await perform(()=>loadActive(active.value!.id),'已载入服务器上的最新草稿。')}
async function settings(){if(busy.value||questionBusy.value||questionPicker.value||!active.value)return;const id=active.value.id;await perform(async()=>{const scope=captureSession();const body:Record<string,unknown>={expectedSettingsRevision:active.value!.settingsRevision,deadlineAt:deadlineInput.value?new Date(deadlineInput.value).toISOString():null};if(!active.value!.policyLocked)body.allowMultiple=allowMultipleInput.value;await request(`/surveys/${id}/settings`,{method:'PATCH',body:JSON.stringify(body)});assertSession(scope);await refreshMetadata()},'收集设置已更新。')}
async function requestCloseSurvey(){if(!active.value||active.value.status!=='open'||busy.value)return;closeConfirmation.value={id:active.value.id,title:active.value.draft.title,revision:active.value.settingsRevision};await nextTick();document.getElementById('cancel-close-survey')?.focus()}
async function closeSurvey(){const confirmation=closeConfirmation.value;closeConfirmation.value=null;if(!confirmation||active.value?.id!==confirmation.id||busy.value)return;await perform(async()=>{const scope=captureSession();await post(`/surveys/${confirmation.id}/close`,{expectedSettingsRevision:confirmation.revision});assertSession(scope);if(active.value?.id===confirmation.id)await refreshMetadata()},'问卷已关闭，历史答卷可继续查看。')}
async function runPreview(mode:'interactive'|'final'='interactive'){
  if(!active.value||!definition.value)return
  const scope=captureSession(),check=captureDefinitionCheck(),sequence=++previewCounter
  previewBusy.value=true;designCheckNote.value=''
  const ids=new Set(definition.value.questions.map(q=>q.id)),answers=Object.fromEntries(Object.entries(previewAnswers.value).filter(([id])=>ids.has(id)))
  try{
    const value=await post<Evaluation>(`/surveys/${active.value.id}/preview`,{definition:definition.value,answers,mode})
    if(isCurrentSession(scope)&&check&&isCurrentDefinitionCheck(check)&&sequence===previewCounter){
      previewResult.value=value
      designCheck.value=Array.isArray(value.warnings)?check:null;designWarnings.value=Array.isArray(value.warnings)?value.warnings:null
      if(!Array.isArray(value.warnings))designCheckNote.value='本次没有返回设计检查结果，请确认服务已更新后重试。'
      if(error.value?.code==='INVALID_DEFINITION'){error.value=null;definitionCheck.value=null}
    }
  }catch(value){
    if(sequence===previewCounter&&check&&isCurrentDefinitionCheck(check)){designCheck.value=null;designWarnings.value=null;showError(value,scope,check)}
  }finally{if(isCurrentSession(scope)&&sequence===previewCounter)previewBusy.value=false}
}
function previewChange(id:string,value:Answer){const scope=captureSession();previewCounter++;previewBusy.value=false;previewAnswers.value={...previewAnswers.value,[id]:value};clearTimeout(previewTimer);previewTimer=setTimeout(()=>{if(isCurrentSession(scope))void runPreview()},220)}
function invalidateDefinitionFeedback(){definitionCounter++;previewCounter++;clearTimeout(previewTimer);previewBusy.value=false;previewResult.value=null;definitionCheck.value=null;designCheck.value=null;designWarnings.value=null;designCheckNote.value='';if(error.value&&isDefinitionError(error.value.code))error.value=null}
watch(definition,invalidateDefinitionFeedback,{deep:true,flush:'sync'})
watch(localQuestionSources,invalidateDefinitionFeedback,{deep:true,flush:'sync'})
watch(()=>[definition.value,editorTab.value],()=>{if(editorTab.value==='preview'&&definition.value){const scope=captureSession();clearTimeout(previewTimer);previewTimer=setTimeout(()=>{if(isCurrentSession(scope))void runPreview()},300)}},{deep:true})
async function openHistory(){if(busy.value||questionBusy.value||questionPicker.value)return;await perform(async()=>{const scope=captureSession();await loadVersions();assertSession(scope);rightVersionId.value=versions.value[0]?.id||'';leftVersionId.value=versions.value[1]?.id||versions.value[0]?.id||'';showHistory.value=true})}
async function copyVersion(version:Version){if(busy.value||questionBusy.value||questionPicker.value||!active.value||!confirm(`把 V${version.versionNo} 复制为当前草稿？原有未保存编辑将被替换，历史版本不会被修改。`))return;const id=active.value.id;await perform(async()=>{const scope=captureSession();await request(`/surveys/${id}/draft`,{method:'PUT',body:JSON.stringify({definition:version.definition,expectedDraftRevision:active.value!.draftRevision,questionSources:version.questionSources??[]})});assertSession(scope);await loadActive(id);assertSession(scope);showHistory.value=false;page.value='editor';editorTab.value='design'},'历史定义已复制为新草稿，发布后才影响新填写。')}
async function showStats(){if(busy.value||questionBusy.value||questionPicker.value||!active.value)return;if(dirty.value&&!confirm('统计使用已发布版本，未保存草稿会保留在本页。继续？'))return;page.value='statistics';statsVersion.value=active.value.currentVersionId||versions.value[0]?.id||'';statsQuestion.value='';statsRespondentType.value='all';await perform(refreshStats)}
async function refreshStats(resetRange=true){
  const sequence=++statsCounter;detailCounter++
  if(!active.value||!statsVersion.value){stats.value=null;detailItems.value=[];nextDetailCursor.value=null;return}
  const scope=captureSession(),surveyId=active.value.id,versionId=statsVersion.value,respondentType=statsRespondentType.value
  const params=new URLSearchParams({versionId,respondentType})
  const boundary=statsBoundary?.surveyId===surveyId&&statsBoundary.versionId===versionId?statsBoundary.asOfSeq:stats.value?.versionId===versionId?stats.value.asOfSeq:null
  if(boundary!=null)statsBoundary={surveyId,versionId,asOfSeq:boundary}
  if(!resetRange&&boundary!=null)params.set('asOfSeq',String(boundary))
  stats.value=null;detailItems.value=[];nextDetailCursor.value=null
  const current=()=>isCurrentSession(scope)&&sequence===statsCounter&&active.value?.id===surveyId&&statsVersion.value===versionId&&statsRespondentType.value===respondentType
  try{
    const value=await request<Stats>(`/surveys/${surveyId}/stats?${params}`)
    if(!current())return
    statsBoundary={surveyId,versionId,asOfSeq:value.asOfSeq};stats.value=value;await loadDetails(true)
  }catch(value){if(current())throw value}
}
async function loadDetails(reset=false){
  if(!active.value||!stats.value)return
  const scope=captureSession(),snapshot=stats.value,surveyId=active.value.id,questionId=statsQuestion.value,sequence=++detailCounter
  const params=new URLSearchParams({versionId:snapshot.versionId,respondentType:snapshot.respondentType,asOfSeq:String(snapshot.asOfSeq),limit:'20'})
  if(questionId)params.set('questionId',questionId)
  if(!reset&&nextDetailCursor.value)params.set('cursor',nextDetailCursor.value)
  if(reset){detailItems.value=[];nextDetailCursor.value=null}
  const current=()=>isCurrentSession(scope)&&sequence===detailCounter&&active.value?.id===surveyId&&stats.value===snapshot&&statsVersion.value===snapshot.versionId&&statsRespondentType.value===snapshot.respondentType&&statsQuestion.value===questionId
  try{
    const value=await request<{items:ResponseItem[];nextCursor?:string|null}>(`/surveys/${surveyId}/responses?${params}`)
    if(!current())return
    detailItems.value=reset?value.items:[...detailItems.value,...value.items];nextDetailCursor.value=value.nextCursor||null
  }catch(value){if(current())throw value}
}
async function exportCSV(){
  if(!active.value||!stats.value)return
  const snapshot=stats.value,surveyId=active.value.id,scope=captureSession()
  const current=()=>isCurrentSession(scope)&&active.value?.id===surveyId&&stats.value===snapshot&&statsVersion.value===snapshot.versionId&&statsRespondentType.value===snapshot.respondentType
  if(!current())return
  const params=new URLSearchParams({versionId:snapshot.versionId,respondentType:snapshot.respondentType,asOfSeq:String(snapshot.asOfSeq)})
  const suffix=snapshot.respondentType==='all'?'':snapshot.respondentType==='anonymous'?'-匿名':'-实名'
  await perform(async()=>{await download(`/surveys/${surveyId}/export?${params}`,`${statisticsTitle.value}-V${selectedVersion.value?.versionNo||''}${suffix}-范围${snapshot.asOfSeq}.csv`,current)},'CSV 导出已开始，使用当前填写方式和固定范围，包含全部题目。')
}
function answerDisplay(item:{questionId:string;value:Answer}){const q=selectedVersion.value?.definition.questions.find(x=>x.id===item.questionId);if(q?.kind==='single')return q.config.options?.find(o=>o.id===item.value)?.label||String(item.value);if(Array.isArray(item.value))return item.value.map(id=>q?.config.options?.find(o=>o.id===id)?.label||id).join('、');return String(item.value??'未答')}

async function loadPublic(){const scope=captureSession();const value=await request<Record<string,unknown>>(`/survey/${encodeURIComponent(publicId)}`);assertSession(scope);publicInfo.value=value}
async function startFill(){await perform(async()=>{const scope=captureSession();const value=await post<{fillToken:string;versionId:string;versionNo:number;expiresAt:string;definition:Definition}>(`/survey/${encodeURIComponent(publicId)}/start`,{});assertSession(scope);fillToken.value=value.fillToken;fillVersionId.value=value.versionId;fillVersionNo.value=value.versionNo;fillExpiresAt.value=value.expiresAt;fillDefinition.value=value.definition;fillAnswers.value={};fillResult.value=null;fillCurrentId.value=value.definition.entryQuestionId||'';anonymous.value=value.definition.allowAnonymous;receipt.value=null;touched.value=new Set();attemptedNext.value=false;await evaluateFill(++evaluateCounter)})}
async function evaluateFill(sequence:number):Promise<Evaluation|undefined>{if(!fillDefinition.value)return;const scope=captureSession();fillBusy.value=true;try{const value=await post<Evaluation>(`/survey/${encodeURIComponent(publicId)}/evaluate`,{versionId:fillVersionId.value,answers:fillAnswers.value},{'X-Fill-Token':fillToken.value});if(!isCurrentSession(scope)||sequence!==evaluateCounter)return;fillResult.value=value;if(!value.path.includes(fillCurrentId.value))fillCurrentId.value=value.blockedAt||value.path[0]||fillDefinition.value?.entryQuestionId||'';return value}catch(value){if(sequence===evaluateCounter)showError(value,scope)}finally{if(isCurrentSession(scope)&&sequence===evaluateCounter)fillBusy.value=false}}
function fillChange(id:string,value:Answer){const scope=captureSession();fillAnswers.value={...fillAnswers.value,[id]:value};touched.value.add(id);const sequence=++evaluateCounter;clearTimeout(fillTimer);fillTimer=setTimeout(()=>{if(isCurrentSession(scope))void evaluateFill(sequence)},220)}
async function nextQuestion(){const scope=captureSession();attemptedNext.value=true;clearTimeout(fillTimer);const value=await evaluateFill(++evaluateCounter);if(!isCurrentSession(scope)||!value||value.errors.some(e=>e.questionId===fillCurrentId.value))return;const index=value.path.indexOf(fillCurrentId.value);const next=value.path[index+1];if(next&&next!=='END'){fillCurrentId.value=next;attemptedNext.value=false}else if(value.complete)notice.value='已到达问卷末尾，可以确认并提交。'}
function previousQuestion(){const previous=fillPath.value[currentIndex.value-1];if(previous){fillCurrentId.value=previous;attemptedNext.value=false;clearFeedback()}}
async function submitFill(retry=false){if(!retry&&!fillDefinition.value)return;await perform(async()=>{
  const scope=captureSession()
  if(!retry){clearTimeout(fillTimer);const result=await evaluateFill(++evaluateCounter);assertSession(scope);if(!result)return;if(!result.complete||result.errors.length){attemptedNext.value=true;if(result?.blockedAt)fillCurrentId.value=result.blockedAt;throw new Error('还有需要完成或修正的问题，请检查后提交。')}pending.value={key:uid('sub'),body:{versionId:fillVersionId.value,anonymous:anonymous.value,answers:clone(fillAnswers.value)},fillToken:fillToken.value};rememberPending()}
  if(!pending.value)throw new Error('没有可恢复的提交。')
  const submission=pending.value
  try{const accepted=await post<Receipt>(`/survey/${encodeURIComponent(publicId)}/submit`,submission.body,{'Idempotency-Key':submission.key,...(submission.fillToken?{'X-Fill-Token':submission.fillToken}:{})});assertSession(scope);receipt.value=accepted;pending.value=null;rememberPending();await loadPublic()}
  catch(value){if(isCurrentSession(scope)&&value instanceof ApiError&&value.status>=400&&value.status<500&&value.status!==401&&value.code!=='IDEMPOTENCY_CONFLICT'){pending.value=null;rememberPending()}throw value}
})}
function discardPending(){if(!confirm('原提交可能已经成功。放弃恢复后，新填写仍受重复填写政策约束。确定继续？'))return;pending.value=null;rememberPending();fillDefinition.value=null;clearFeedback()}

function beforeUnload(event:BeforeUnloadEvent){if(dirty.value||questionLibrary.value?.hasUnsavedChanges()||questionBusy.value||(!receipt.value&&fillDefinition.value&&Object.keys(fillAnswers.value).length)){event.preventDefault();event.returnValue=''}}
function escapeDialog(event:KeyboardEvent){if(event.key==='Escape'){if(questionPicker.value){closeQuestionPicker();return}showHistory.value=false;showHelp.value=false;closeConfirmation.value=null}}
function sessionChanged(event:StorageEvent){
  if(event.storageArea!==localStorage||(event.key!==null&&event.key!=='wenchuan_token'))return
  // Another page owns the new token. Clear this page without removing that token.
  clearAccountViews();error.value=new ApiError(401,'SESSION_CHANGED','登录状态已在其他页面变更，请重新登录。')
}
onMounted(async()=>{const scope=captureSession();window.addEventListener('beforeunload',beforeUnload);window.addEventListener('keydown',escapeDialog);window.addEventListener('storage',sessionChanged);try{if(scope.token){const account=await request<User>('/auth/me');assertSession(scope);user.value=account;if(publicId){restorePending();await loadPublic()}else await loadSurveys()}}catch(value){showError(value,scope)}finally{if(isCurrentSession(scope))booting.value=false}})
onUnmounted(()=>{sessionGeneration++;clearTimeout(fillTimer);clearTimeout(previewTimer);window.removeEventListener('beforeunload',beforeUnload);window.removeEventListener('keydown',escapeDialog);window.removeEventListener('storage',sessionChanged)})
</script>

<template>
  <div v-if="booting" class="boot-screen"><div class="brand-icon">川</div><div class="spinner"/><p>正在打开问卷工作台</p></div>
  <div v-else-if="!user" class="auth-layout">
    <div class="auth-story"><a href="/" class="brand"><span class="brand-icon">川</span><span>问川</span></a><div class="auth-story-copy"><h1>问卷管理</h1><p>创建问卷、设置跳转，<br/>发布后查看统计和原始回答。</p></div></div>
    <main class="auth-form-wrap"><div class="auth-form"><span class="eyebrow">欢迎来到问川</span><h2>{{authMode==='login'?'登录问川':'注册账号'}}</h2><p class="muted">{{publicId?'登录后将返回你收到的问卷邀请。':'登录后管理你的问卷。'}}</p><div class="segmented auth-tabs"><button :class="{active:authMode==='login'}" @click="authMode='login';clearFeedback()">登录</button><button :class="{active:authMode==='register'}" @click="authMode='register';clearFeedback()">注册账号</button></div><form @submit.prevent="authenticate"><label>用户名<input v-model="username" autocomplete="username" minlength="3" maxlength="32" required placeholder="请输入用户名"/></label><label>密码<input v-model="password" type="password" :autocomplete="authMode==='login'?'current-password':'new-password'" minlength="8" maxlength="128" required placeholder="至少 8 个字符"/></label><p v-if="authMode==='register'" class="hint">用户名支持中文、字母、数字及下划线，长度 3—32 字。</p><div v-if="error" class="feedback error" role="alert">{{error.message}}<small v-for="(item,i) in error.fieldErrors" :key="i">{{item.message}}</small></div><button class="button primary full-width" type="submit" :disabled="busy">{{busy?'请稍候…':authMode==='login'?'登录':'创建账号'}}</button></form><p class="auth-security">你的账号用于管理问卷。匿名填写时，身份会对创建者隐藏。</p></div></main>
  </div>
  <div v-else-if="publicId" class="fill-layout">
    <header class="fill-header"><a href="/" class="brand"><span class="brand-icon">川</span><span>问川</span></a><div class="button-row"><span class="small muted">{{user.username}}</span><button class="text-button" @click="logout">退出</button></div></header>
    <main class="fill-main">
      <div v-if="error" class="feedback error" role="alert"><strong>{{friendlyCodes[error.code]||error.message}}</strong><small v-for="(item,i) in error.fieldErrors" :key="i">{{item.message}}</small><small v-if="error.requestId">请求 {{error.requestId}}</small><button class="text-button small" @click="error=null">收起</button></div>
      <div v-if="notice" class="feedback success" role="status">{{notice}}</div>
      <div v-if="receipt" class="panel receipt-card"><div class="success-orbit">✓</div><span class="eyebrow">答卷已保存</span><h1>提交成功</h1><p v-if="fillDefinition&&receipt.versionId===fillVersionId" class="muted">你已成功完成「{{fillDefinition.title}}」。</p><p v-else class="muted">答卷已保存，提交时间和回执编号如下。</p><dl class="receipt-details"><div v-if="fillVersionNo!=null&&receipt.versionId===fillVersionId"><dt>问卷版本</dt><dd>V{{fillVersionNo}}</dd></div><div><dt>提交时间</dt><dd>{{date(receipt.acceptedAt)}}</dd></div><div><dt>提交方式</dt><dd>{{receipt.anonymous?'匿名填写':'实名填写'}}</dd></div><div><dt>回执编号</dt><dd><code>{{receipt.receiptId}}</code></dd></div></dl><button class="button secondary" :disabled="busy" @click="startFill">再次填写</button><a href="/" class="text-button">回到我的工作台</a></div>
      <div v-else-if="pending" class="panel pending-card"><span class="eyebrow">提交恢复</span><h1>确认提交结果</h1><p>上次提交的结果尚未确认。请使用相同提交标识恢复结果；即使问卷已经关闭，已成功保存的答卷仍可找回回执。</p><code class="pending-key">{{pending.key}}</code><div class="button-row"><button class="button primary" :disabled="busy" @click="submitFill(true)">{{busy?'确认中…':'使用原标识确认结果'}}</button><button class="text-button" :disabled="busy" @click="discardPending">放弃恢复，重新填写</button></div></div>
      <div v-else-if="!fillDefinition" class="panel invitation-card"><div class="invitation-visual"><span>↗</span><i/><i/></div><span class="eyebrow">一份问卷邀请</span><h1>{{fillTitle}}</h1><p class="invitation-description">{{(publicInfo?.definition as Definition|undefined)?.description || publicInfo?.description || '请按顺序填写，完成后提交。'}}</p><div class="invitation-info"><span>◷ {{publicInfo?.deadlineAt?`截止 ${date(String(publicInfo.deadlineAt))}`:'未设置截止时间'}}</span><span>{{availabilityMessage?'已停止收集':'✓ 已登录，可开始填写'}}</span></div><p v-if="availabilityMessage" class="field-error" role="status">{{availabilityMessage}}</p><button class="button primary" :disabled="busy||!!availabilityMessage||!publicInfo" @click="startFill">{{availabilityMessage?'已停止收集':busy?'准备中…':'开始填写 →'}}</button><p class="hint">匿名政策及具体问题以开始填写时的问卷版本为准。</p></div>
      <template v-else>
        <div class="fill-title-row"><span class="eyebrow">{{fillDefinition.title}}<span v-if="fillVersionNo!=null" class="fill-version"> · 填写版本 V{{fillVersionNo}}</span></span><span class="small muted">{{fillResult?.complete?'已可提交':`当前路径第 ${currentIndex+1} 题`}}</span></div><div class="progress-track"><i :style="{width:`${fillResult?.complete?100:Math.min(96,(currentIndex+1)/Math.max(fillPath.length,fillDefinition.questions.length)*100)}%`}"/></div>
        <section v-if="currentQuestion" class="panel fill-question-card"><div class="question-kicker"><span>{{kindNames[currentQuestion.kind]}}</span><span>{{currentQuestion.required?'必答':'选填'}}</span></div><h1>{{currentQuestion.title}}</h1><AnswerInput :question="currentQuestion" :model-value="fillAnswers[currentQuestion.id]" :disabled="busy" @update:model-value="fillChange(currentQuestion!.id,$event)"/><div v-for="(item,i) in currentErrors" :key="i" class="field-error" role="alert">{{item.message}}</div><div class="fill-actions"><button class="button secondary" :disabled="currentIndex===0||busy" @click="previousQuestion">← 上一题</button><span class="hint" aria-live="polite">{{fillBusy?'正在确认路径…':fillResult?.complete?'所有已到达问题已完成':'回答后自动计算后续路径'}}</span><button v-if="currentIndex<fillPath.length-1" class="button primary" :disabled="busy||fillBusy" @click="nextQuestion">下一题 →</button><button v-else-if="!fillResult?.complete" class="button primary" :disabled="busy||fillBusy" @click="nextQuestion">继续 →</button><button v-else class="button primary" :disabled="busy||fillBusy" @click="submitFill()">确认提交 ✓</button></div></section>
        <div class="fill-bottom"><label v-if="fillDefinition.allowAnonymous" class="checkbox-line"><input v-model="anonymous" type="checkbox" :disabled="busy"/> 匿名提交，向创建者隐藏身份</label><p v-else class="hint">本问卷不支持匿名，提交时创建者可看到你的用户名。</p><details class="path-details"><summary>本次填写路径与判断说明</summary><div class="path-chips"><button v-for="id in fillPath" :key="id" :class="{active:id===fillCurrentId}" @click="fillCurrentId=id">{{questionTitle(id,fillDefinition)}}</button></div><p v-for="(item,i) in fillResult?.trace" :key="i" class="trace-line">{{traceText(item)}}</p></details><p class="hint">填写凭据有效至 {{date(fillExpiresAt)}}。正式提交前可以回看和修改。</p></div>
      </template>
    </main><footer class="fill-footer">问川</footer>
  </div>
  <div v-else class="app-layout">
    <aside class="sidebar"><a href="/" class="brand"><span class="brand-icon">川</span><span>问川</span></a><div class="workspace-caption">个人工作空间</div><nav><button :class="{active:page==='dashboard'}" :disabled="busy||questionBusy||!!questionPicker" @click="goDashboard">我的问卷 <b>{{surveys.length}}</b></button><button :class="{active:page==='questions'}" :disabled="busy||questionBusy||!!questionPicker" @click="openQuestions">个人题目</button><button v-if="active" :class="{active:page==='editor'}" :disabled="busy||questionBusy||!!questionPicker" @click="page='editor'">问卷编辑</button><button v-if="active?.currentVersionId" :class="{active:page==='statistics'}" :disabled="busy||questionBusy||!!questionPicker" @click="showStats">统计与回答</button><button @click="showHelp=true">使用说明</button></nav><div class="user-profile"><span class="avatar">{{user.username.slice(0,1).toUpperCase()}}</span><div><strong>{{user.username}}</strong><small>个人账号</small></div><button class="text-button logout-button" @click="logout">退出</button></div></aside>
    <main class="workspace"><header class="topbar"><div class="breadcrumb"><button :disabled="busy||questionBusy||!!questionPicker" @click="goDashboard">我的问卷</button><template v-if="page==='questions'"><span>/</span><span>个人题目</span></template><template v-if="active"><span>/</span><span>{{active.draft.title}}</span></template></div><span class="date-label">{{new Intl.DateTimeFormat('zh-CN',{month:'long',day:'numeric'}).format(new Date())}}</span></header>
      <div class="workspace-content">
        <div v-if="error" class="feedback error" role="alert"><div><strong>{{friendlyCodes[error.code]||error.message}}</strong><ul v-if="error.fieldErrors.length" class="editor-error-list"><li v-for="(item,i) in error.fieldErrors" :key="i"><span :id="`definition-error-${i}`">{{item.questionId?`${questionTitle(item.questionId)}：`:''}}{{item.message}}</span><button v-if="editorFieldErrors.includes(item)" type="button" class="text-button error-locate" :disabled="busy" @click="focusEditorIssue(item)">定位：{{editorErrorLabel(item)}}</button></li></ul><small v-if="error.requestId">请求 {{error.requestId}}</small><button v-if="active&&error.status===409" class="text-button" :disabled="busy" @click="reloadCurrent">重新载入服务器草稿</button></div><button class="icon-button" aria-label="关闭错误" @click="error=null">×</button></div><div v-if="notice" class="feedback success" role="status"><span>{{notice}}</span><button class="icon-button" aria-label="关闭提示" @click="notice=''">×</button></div>
        <template v-if="page==='dashboard'">
          <div class="page-heading dashboard-heading"><div><h1>我的问卷</h1><p class="hint">管理草稿、收集链接和填写结果。</p></div><div class="button-row"><button class="button secondary" :disabled="busy" @click="createSurvey(true)">从示例创建</button><button class="button primary" :disabled="busy" @click="createSurvey()">＋ 创建问卷</button></div></div>
          <div class="dashboard-summary"><span>已加载 <strong>{{surveys.length}}</strong> 份</span><span>收集中 <strong>{{openCount}}</strong> 份</span><span>未发布 <strong>{{surveys.filter(s=>s.status==='draft').length}}</strong> 份</span><button class="text-button" :disabled="busy" @click="perform(()=>loadSurveys())">刷新列表</button></div>
          <section class="survey-section" aria-label="问卷列表">
            <div class="list-toolbar"><div class="segmented"><button v-for="item in [{id:'all',label:'全部'},{id:'open',label:'收集中'},{id:'draft',label:'草稿'},{id:'closed',label:'已关闭'},{id:'expired',label:'已截止'}]" :key="item.id" :class="{active:statusFilter===item.id}" @click="statusFilter=item.id">{{item.label}}</button></div><input v-model="search" type="search" class="search-input" aria-label="搜索问卷" placeholder="搜索问卷名称或说明"/></div>
            <div v-if="!filteredSurveys.length" class="empty-state"><h3>{{surveys.length?'没有匹配的问卷':'暂无问卷'}}</h3><p>{{surveys.length?'请调整搜索内容或状态筛选。':'点击“创建问卷”开始。'}}</p><button v-if="!surveys.length" class="button primary" :disabled="busy" @click="createSurvey()">创建问卷</button></div>
            <table v-else class="survey-table"><thead><tr><th scope="col">问卷名称</th><th scope="col">状态</th><th scope="col">题目</th><th scope="col">发布版本</th><th scope="col" class="survey-actions-heading">操作</th></tr></thead><tbody><tr v-for="survey in filteredSurveys" :key="survey.id"><td class="survey-name"><button class="survey-title" @click="openSurvey(survey.id)">{{survey.draft.title || '未命名问卷'}}</button><p v-if="survey.draft.description" class="survey-description">{{survey.draft.description}}</p><small class="survey-created">创建于 {{date(survey.createdAt)}}</small></td><td class="survey-status"><span :class="['status-pill',statusOf(survey)]">{{statusName(survey)}}</span></td><td class="survey-count">{{survey.draft.questions.length}} 题</td><td class="survey-version">{{survey.lastVersionNo?`V${survey.lastVersionNo}`:'未发布'}}</td><td class="survey-actions"><div><button class="text-button" @click="openSurvey(survey.id)">编辑</button><button v-if="survey.currentVersionId" class="text-button" @click="openSurvey(survey.id,'statistics')">统计</button><button v-if="survey.currentVersionId" class="text-button" @click="copyShare(survey)">复制链接</button></div></td></tr></tbody></table>
            <button v-if="nextSurveyCursor" class="button secondary load-more" :disabled="busy" @click="perform(()=>loadSurveys(true))">加载更多问卷</button>
          </section>
        </template>
        <QuestionLibrary v-else-if="page==='questions'" :key="user.id" ref="questionLibrary" @busy-change="questionBusy=$event" @unauthorized="expireQuestionSession"/>
        <fieldset v-else-if="active&&definition&&page==='editor'" class="editor-operation" :disabled="busy||questionBusy||!!questionPicker"><div class="page-heading"><div><div class="eyebrow">草稿修订 {{active.draftRevision}}</div><h1>{{definition.title || '未命名问卷'}}<span v-if="dirty" class="dirty-dot" title="有未保存的修改"/></h1><p class="hint">{{dirty?'有未保存的修改。预览可使用当前内容，发布使用已保存草稿。':'草稿已保存，发布后供填写者使用。'}}</p></div><div class="button-row"><button class="button secondary" :disabled="busy" @click="openHistory">版本历史</button><button class="button secondary" :disabled="busy||!dirty" @click="saveDraft">{{busy?'处理中…':'保存草稿'}}</button><button class="button primary" :disabled="busy||dirty||remoteDraftChanged||!definition.questions.length" @click="publish">发布新版本</button></div></div>
          <div v-if="remoteDraftChanged" class="feedback error" role="status"><strong>服务器上的草稿已在其他页面修改。本页内容和引用仍保留，请重新载入并比较后发布。</strong><button class="button secondary small" :disabled="busy" @click="reloadCurrent">重新载入草稿</button></div><div class="editor-subnav"><div class="tabs"><button :class="{active:editorTab==='design'}" @click="editorTab='design'">编辑题目</button><button :class="{active:editorTab==='preview'}" @click="editorTab='preview'">填写预览</button><button :class="{active:editorTab==='settings'}" @click="editorTab='settings'">收集设置</button></div><div class="button-row"><span :class="['status-pill',statusOf(active)]"><i/>{{statusName(active)}}</span><button v-if="active.currentVersionId" class="text-button" @click="copyShare(active)">复制填写链接</button></div></div>
          <section v-if="editorTab!=='settings'" class="panel design-checks" aria-labelledby="design-checks-title">
            <div class="section-heading"><div><h2 id="design-checks-title">设计检查</h2><p class="hint">检查当前草稿的不可达题与明显规则冲突。提示不会阻止发布。</p></div><button class="button secondary small" :disabled="busy||previewBusy" @click="runPreview()">{{previewBusy?'检查中…':'检查设计'}}</button></div>
            <p v-if="currentDesignWarnings===null" class="hint">{{previewBusy?'正在检查当前内容。':designCheckNote||'编辑后请重新检查，也可在填写预览中自动检查。'}}</p>
            <ul v-else-if="currentDesignWarnings.length" class="design-warning-list"><li v-for="(item,index) in currentDesignWarnings" :key="index"><span :id="`design-warning-${index}`">{{item.message}}</span><small v-if="relatedRulesLabel(item)" class="design-warning-related">先匹配规则：{{relatedRulesLabel(item)}}</small><button class="text-button error-locate" @click="focusDesignWarning(item)">{{editorErrorLabel(item)}} →</button></li></ul>
            <p v-else class="hint" role="status">在已支持的检查范围内，未发现设计提示。</p>
            <details class="design-check-scope"><summary>检查范围</summary><p class="hint">不可达题按入口与全部跳转连线判断；规则按优先级检查。单选、数字检查可判定的匹配范围，多选检查明确重复的条件。没有提示不代表所有设计都合理，仍需用样例回答预览。</p></details>
          </section>
          <DefinitionEditor v-if="editorTab==='design'" :key="active.id" ref="definitionEditor" v-model="definition" :errors="editorFieldErrors" :warnings="currentDesignWarnings||[]" :sources="currentQuestionSources" @pick-question="openQuestionPicker()" @switch-version="openQuestionPicker($event)" @detach-source="detachQuestion"/>
          <div v-else-if="editorTab==='preview'" class="preview-layout"><div class="panel"><div class="section-heading"><div><h2>填写预览</h2><p class="hint">输入回答，检查跳转和校验结果。</p></div><span class="status-pill draft">{{previewBusy?'正在计算':'实时预览'}}</span></div><template v-for="(q,index) in definition.questions" :key="q.id"><div v-if="(previewResult?.path||[definition.entryQuestionId]).includes(q.id)" class="preview-question"><div class="question-kicker"><span>{{String(index+1).padStart(2,'0')}} · {{kindNames[q.kind]}}</span><span>{{q.required?'必答':'选填'}}</span></div><h3>{{q.title}}</h3><AnswerInput :question="q" :model-value="previewAnswers[q.id]" @update:model-value="previewChange(q.id,$event)"/></div></template><div class="button-row"><button class="button secondary" :disabled="previewBusy" @click="runPreview('final')">完整校验</button><button class="text-button" @click="previewAnswers={};runPreview()">清空样例回答</button></div></div><aside class="panel trace-panel"><h2>跳转说明</h2><div class="path-stack"><div v-for="(id,index) in previewResult?.path||[]" :key="id"><b>{{index+1}}</b><span>{{questionTitle(id)}}</span></div><div v-if="previewResult?.complete" class="path-complete">✓ 到达结束节点</div></div><div v-if="previewResult?.blockedAt" class="feedback neutral">等待回答：{{questionTitle(previewResult.blockedAt)}}</div><p v-for="(item,index) in previewResult?.trace||[]" :key="index" class="trace-line">{{traceText(item)}}</p><div v-for="(item,index) in previewResult?.errors||[]" :key="index" class="field-error">{{item.message}}</div><details v-if="previewResult"><summary>查看后端求值结果</summary><pre>{{JSON.stringify(previewResult,null,2)}}</pre></details></aside></div>
          <div v-else class="settings-layout"><section class="panel"><h2>收集设置</h2><p class="hint">设置影响全部发布版本；题目与匿名政策由各自发布快照保留。</p><label>截止时间<input v-model="deadlineInput" type="datetime-local" :disabled="active.status==='closed'"/><small>以你的本地时区显示；留空表示不设置截止时间。</small></label><label class="switch-line"><input v-model="allowMultipleInput" type="checkbox" :disabled="active.policyLocked||active.status==='closed'"/><span><strong>允许同一个人多次填写</strong><small>{{active.policyLocked?'已经收到正式答卷，此政策已锁定。':'首份正式答卷提交后，此政策将锁定，发布新版本不会重置。'}}</small></span></label><button class="button primary" :disabled="busy||active.status==='closed'" @click="settings">保存收集设置</button></section><aside class="panel"><h3>填写链接</h3><p class="hint">链接始终指向当前发布版本。填写者必须登录。</p><input :value="shareURL(active)" readonly aria-label="公开填写链接" @focus="($event.target as HTMLInputElement).select()"/><button class="button secondary full-width" :disabled="!active.currentVersionId" @click="copyShare(active)">复制链接</button><a v-if="active.currentVersionId" :href="shareURL(active)" target="_blank" rel="noopener" class="text-button">打开实际填写页面 ↗</a><div class="danger-zone"><h3>停止收集</h3><p class="hint">关闭后不能重新开启。已有答卷和成功回执仍会保留。</p><button class="button danger" :disabled="busy||active.status!=='open'" @click="requestCloseSurvey">关闭问卷</button></div></aside></div>
        </fieldset>
        <template v-else-if="active&&page==='statistics'"><div class="page-heading"><div><h1>统计与回答</h1><p class="hint">{{statisticsTitle}} · 统计与明细使用同一固定范围。</p></div><div class="button-row"><select v-model="statsVersion" aria-label="选择统计版本" :disabled="busy" @change="statsQuestion='';perform(refreshStats)"><option v-for="version in versions" :key="version.id" :value="version.id">V{{version.versionNo}} · {{date(version.publishedAt)}}</option></select><select v-model="statsRespondentType" aria-label="筛选填写方式" :disabled="busy" @change="perform(()=>refreshStats(false))"><option value="all">全部填写方式</option><option value="anonymous">仅匿名填写</option><option value="named">仅实名填写</option></select><button class="button secondary" :disabled="busy||!stats" @click="exportCSV">↓ 导出本版本 CSV</button><button class="button primary" :disabled="busy||!statsVersion" @click="perform(refreshStats)">↻ 刷新范围</button></div></div>
          <div v-if="!stats" class="panel empty-state"><div class="empty-symbol">▥</div><h3>{{busy?'正在加载统计…':statsVersion?'统计尚未加载':'暂无已发布版本'}}</h3><p>{{busy?'正在读取当前筛选结果。':statsVersion?'请刷新范围重试。':'先发布一个版本，再分享给填写者。'}}</p></div>
          <template v-else><div class="metric-grid stats-metrics"><div class="metric-card"><span>有效答卷</span><strong>{{stats.totalResponses}}<small>份</small></strong><i>▤</i></div><div class="metric-card"><span>独立参与者</span><strong>{{stats.uniqueParticipants}}<small>人</small></strong><i>◈</i></div><div class="metric-card"><span>匿名答卷</span><strong>{{stats.anonymousResponses}}<small>份</small></strong><i>◉</i></div><div class="metric-card"><span>当前版本</span><strong>V{{stats.versionNo||selectedVersion?.versionNo||'—'}}</strong><i>↗</i></div></div><div class="list-toolbar"><p class="hint">{{respondentTypeNames[stats.respondentType]}} · 范围截至提交序号 <strong>{{stats.asOfSeq}}</strong> · 新答卷不会改变当前分页与导出。</p><select v-model="statsQuestion" aria-label="筛选单题统计" :disabled="busy" @change="perform(()=>loadDetails(true))"><option value="">查看整个问卷</option><option v-for="q in stats.questions" :key="q.questionId" :value="q.questionId">{{q.title}}</option></select></div>
            <div class="statistics-grid"><article v-for="q in displayedQuestions" :key="q.questionId" class="panel stats-question"><div class="section-heading"><span class="eyebrow">{{kindNames[q.kind]}}</span><span class="small muted">有效回答 {{q.A}} 份</span></div><h3>{{q.title}}</h3><div class="answer-breakdown"><span>到达 {{q.V}}</span><span>未答 {{q.U}}</span><span>跳过 {{q.S}}</span></div><div v-if="q.kind==='single'||q.kind==='multi'" class="bar-chart"><div v-for="option in q.options||[]" :key="option.id" class="bar-row"><div><span>{{option.label}}</span><strong>{{option.count}} <small>次 · {{q.A?Math.round(option.count/q.A*100):0}}%</small></strong></div><div class="bar-track"><i :style="{width:`${q.A?Math.min(100,option.count/q.A*100):0}%`}"/></div><small v-if="option.uniqueParticipants!=null" class="muted">独立参与者 {{option.uniqueParticipants}} 人</small></div><p class="hint">以有效回答答卷数为分母{{q.kind==='multi'?'；多选比例合计可以超过 100%。':'。'}}</p></div><div v-else-if="q.kind==='number'" class="numeric-stats"><div><span>平均值</span><strong>{{q.avg==null?'暂无数据':Number(q.avg.toFixed(2))}}</strong></div><div><span>总和</span><strong>{{q.sum??'—'}}</strong></div><p class="hint">数字 0 计入均值；未答与跳过不计入。原始数值可在下方明细查看。</p></div><div v-else class="text-stats"><div class="quote-mark">“</div><p>{{q.A?'原始文本可在下方明细中查看。':'还没有有效文本回答。'}}</p><button class="text-button" @click="statsQuestion=q.questionId;perform(()=>loadDetails(true))">查看文本明细 →</button></div></article></div>
            <section class="panel response-panel"><div class="section-heading"><div><h2>原始回答明细</h2><p class="hint">文本和数字均逐份展示，匿名答卷不会显示身份。</p></div><span class="status-pill draft">已加载 {{detailItems.length}} 份</span></div><div v-if="!detailItems.length" class="empty-state compact"><p>{{statsQuestion?'当前题目在此范围内没有有效回答。':'当前范围还没有答卷。'}}</p></div><div v-for="item in detailItems" :key="item.receiptId" class="response-row"><div class="response-heading"><span class="avatar small-avatar">{{item.anonymous?'匿':(item.respondentName||'实').slice(0,1)}}</span><div><strong>{{item.anonymous?'匿名填写':item.respondentName||'实名填写'}}</strong><small>{{date(item.acceptedAt)}} · 第 {{item.acceptedSeq}} 份</small></div><code>{{item.receiptId.slice(0,12)}}…</code></div><dl><template v-for="answer in item.answers.filter(a=>!statsQuestion||a.questionId===statsQuestion)" :key="answer.questionId"><dt>{{questionTitle(answer.questionId,selectedVersion?.definition||null)}}</dt><dd>{{answerDisplay(answer)}}</dd></template></dl><p v-if="!item.answers.length" class="hint">本份答卷的已到达问题均为选填且未回答。</p></div><button v-if="nextDetailCursor" class="button secondary load-more" :disabled="busy" @click="perform(()=>loadDetails())">加载更多明细</button></section>
          </template>
        </template>
      </div>
    </main>
    <div v-if="closeConfirmation" class="modal-backdrop" @click.self="closeConfirmation=null"><section class="modal help-modal" role="alertdialog" aria-modal="true" aria-labelledby="close-survey-title" aria-describedby="close-survey-description" @keydown.tab.prevent="($event.target === $refs.closeCancel ? ($refs.closeConfirm as HTMLButtonElement) : ($refs.closeCancel as HTMLButtonElement))?.focus()"><h2 id="close-survey-title">关闭「{{closeConfirmation.title}}」？</h2><p id="close-survey-description">关闭后不能重新开启，也不再接受新答卷。已有答卷和成功回执仍会保留。</p><div class="button-row"><button id="cancel-close-survey" ref="closeCancel" class="button secondary" @click="closeConfirmation=null">取消关闭</button><button ref="closeConfirm" class="button danger" :disabled="busy" @click="closeSurvey">确认关闭</button></div></section></div>
    <div v-if="questionPicker" class="modal-backdrop question-picker-backdrop"><section class="modal question-picker-modal" role="dialog" aria-modal="true" aria-labelledby="question-picker-title"><div class="section-heading"><div><h2 id="question-picker-title">{{questionPicker.instanceId?'更换引用版本':'从个人题目选用'}}</h2><p class="hint">选择已保存的版本；加入后仍需保存草稿并发布。</p></div><button class="icon-button" aria-label="关闭题目选择" :disabled="questionBusy" @click="closeQuestionPicker">×</button></div><QuestionLibrary mode="pick" :question-id="pickerSource?.questionId" :version-id="pickerSource?.questionVersionId" @choose="chooseQuestionVersion" @busy-change="questionBusy=$event" @unauthorized="expireQuestionSession"/></section></div>
    <div v-if="showHistory" class="modal-backdrop" @click.self="showHistory=false"><section class="modal history-modal" role="dialog" aria-modal="true" aria-labelledby="history-title"><div class="section-heading"><div><h2 id="history-title">版本历史</h2></div><button class="icon-button" aria-label="关闭版本历史" @click="showHistory=false">×</button></div><div v-if="!versions.length" class="empty-state"><p>保存并发布后，你的第一个版本会出现在这里。</p></div><template v-else><div class="version-list"><div v-for="version in versions" :key="version.id" class="version-row"><div class="version-badge">V{{version.versionNo}}</div><div><strong>{{version.definition.title}}</strong><small>{{date(version.publishedAt)}} · 来源草稿 r{{version.sourceDraftRevision}}</small></div><button class="button secondary small" :disabled="busy" @click="copyVersion(version)">复制为新草稿</button><details><summary>查看完整定义</summary><pre>{{JSON.stringify(version.definition,null,2)}}</pre></details></div></div><div class="version-comparison"><h3>按稳定标识比较版本</h3><div class="inline-fields"><select v-model="leftVersionId" aria-label="比较旧版本"><option v-for="version in versions" :key="version.id" :value="version.id">V{{version.versionNo}}</option></select><span>→</span><select v-model="rightVersionId" aria-label="比较新版本"><option v-for="version in versions" :key="version.id" :value="version.id">V{{version.versionNo}}</option></select></div><p v-if="!differences.length" class="hint">这两个定义没有差异。</p><div v-for="(diff,index) in differences" :key="index" class="diff-row"><span class="status-pill draft">{{diff.kind}}</span><strong>{{diff.title}}</strong><details><summary>展开变化内容</summary><div class="diff-values"><pre v-if="diff.before!==undefined">旧：{{JSON.stringify(diff.before,null,2)}}</pre><pre v-if="diff.after!==undefined">新：{{JSON.stringify(diff.after,null,2)}}</pre></div></details></div></div></template></section></div>
    <div v-if="showHelp" class="modal-backdrop" @click.self="showHelp=false"><section class="modal help-modal" role="dialog" aria-modal="true" aria-labelledby="help-title"><div class="section-heading"><h2 id="help-title">使用说明</h2><button class="icon-button" aria-label="关闭指南" @click="showHelp=false">×</button></div><ol class="guide-list"><li><strong>添加题目与约束</strong><p>新建问卷，添加单选、多选、文本和数字问题，为问题设置必要的约束。</p></li><li><strong>配置并预览跳转</strong><p>配置条件和默认去向，使用实时预览检查路径与校验提示。优先级数值较小的规则先执行。</p></li><li><strong>保存、发布，再分享</strong><p>草稿保存后才能发布。每次发布保留完整快照，已经开始的填写继续使用原版本。</p></li><li><strong>查看统计与明细</strong><p>按版本查看答卷、独立人数及原始内容。刷新统计会创建新的固定范围，导出与明细沿用这个范围。</p></li></ol></section></div>
  </div>
</template>

