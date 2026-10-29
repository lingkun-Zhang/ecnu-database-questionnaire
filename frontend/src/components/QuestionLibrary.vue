<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import AnswerInput from './AnswerInput.vue'
import QuestionSharePanel from './QuestionSharePanel.vue'
import QuestionImpactReview from './QuestionImpactReview.vue'
import { ApiError, getToken, post, request } from '../api'
import { clone, kindNames, newQuestion, uid } from '../types'
import type { Answer, FieldError, Kind, Question, QuestionContent, QuestionUsage, QuestionVersion, ReusableQuestion } from '../types'

const props=withDefaults(defineProps<{mode?:'manage'|'pick';questionId?:string;versionId?:string;destination?:'survey'|'bank'}>(),{mode:'manage',destination:'survey'})
const emit=defineEmits<{choose:[QuestionVersion];'busy-change':[boolean];unauthorized:[];statistics:[questionId:string]}>()
interface CurrentVersionSummary {id:string;questionId:string;versionNo:number;content:Pick<QuestionContent,'kind'|'title'>}
interface ListQuestion extends ReusableQuestion {currentVersion:CurrentVersionSummary}
interface Entry { root:ReusableQuestion; current:CurrentVersionSummary }
interface Scope { token:string; generation:number }
interface HistoryPage { items:QuestionVersion[];offset:number;limit:number }
interface PendingWrite { kind:'create'|'derive'|'restore';key:string;path:string;body:Record<string,unknown>;result?:QuestionVersion;uncertain:boolean }
interface ImpactDecision {id:string;questionId:string;sourceVersionId:string;sourceVersionLabel:string;kind:'derive'|'restore';path:string;body:Record<string,unknown>;session:Scope}
interface ImpactApproval {decisionId:string;questionId:string;sourceVersionId:string;checkedAt:string}
const impactDecision=ref<ImpactDecision|null>(null),impactBusy=ref(false)
const libraryRoot=ref<HTMLElement|null>(null)
const busy=ref(false),failure=ref<ApiError|null>(null),note=ref('')
const collection=ref<'owned'|'shared'>('owned'),sharedCursors=ref<Array<string|null>>([null])
const sharePanel=ref<{canLeave:()=>boolean;hasUnsavedChanges:()=>boolean}|null>(null),sharingBusy=ref(false)
const operationBusy=computed(()=>busy.value||sharingBusy.value||impactBusy.value)
const entries=ref<Entry[]>([]),listOffset=ref(0),listMore=ref(false),listLoaded=ref(false)
const selectedRoot=ref<ReusableQuestion|null>(null),history=ref<QuestionVersion[]>([])
const historyOffset=ref(0),historyMore=ref(false),selectedVersionId=ref('')
const editor=ref<QuestionContent|null>(null),editBaseline=ref(''),editParentVersionId=ref(''),editExpectedHeadId=ref('')
const pendingWrite=ref<PendingWrite|null>(null),refreshNeeded=ref(false),conflictNeedsReview=ref(false)
const usageScope=ref<'draft'|'published'>('draft'),usageVersion=ref<'all'|'selected'>('all')
const usageRows=ref<QuestionUsage[]>([]),usageNext=ref<string|null>(null),usageLoaded=ref(false)
const previewAnswer=ref<Answer>(null),previewId=uid('library_preview')
let generation=0,alive=true
class StaleLibraryResponse extends Error {}
const selectedVersion=computed(()=>history.value.find(item=>item.id===selectedVersionId.value)||null)
const dirty=computed(()=>editor.value!==null&&JSON.stringify(editor.value)!==editBaseline.value&&!pendingWrite.value?.result)
const editorLocked=computed(()=>operationBusy.value||Boolean(impactDecision.value||pendingWrite.value?.uncertain||pendingWrite.value?.result))
const ownerSelection=computed(()=>props.mode==='manage'&&collection.value==='owned'&&Boolean(selectedRoot.value&&entries.value.some(entry=>entry.root.id===selectedRoot.value!.id)))
const lockedQuestion=computed(()=>props.mode==='pick'&&Boolean(props.questionId))
const previewQuestion=computed<Question|null>(()=>{
  const content=editor.value||selectedVersion.value?.content
  return content?{id:previewId,...clone(content),required:false,defaultTarget:'END',rules:[]}:null
})
function capture():Scope{return {token:getToken(),generation}}
function current(scope:Scope){return alive&&scope.generation===generation&&scope.token===getToken()}
function check(scope:Scope){if(!current(scope))throw new StaleLibraryResponse()}
function pathQuestion(id:string){return `/questions/${encodeURIComponent(id)}`}
function formatDate(value:string){return new Intl.DateTimeFormat('zh-CN',{year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'}).format(new Date(value))}
function versionLabel(id:string|null|undefined){if(!id)return '初始版本';const item=history.value.find(value=>value.id===id);return item?`V${item.versionNo}`:'未载入的历史版本'}
function emitBusy(){emit('busy-change',operationBusy.value)}
function onShareBusy(value:boolean){sharingBusy.value=value;emitBusy()}
function onImpactBusy(value:boolean){impactBusy.value=value;emitBusy()}
function openStatistics(){if(operationBusy.value||!selectedRoot.value)return;emit('statistics',selectedRoot.value.id)}
function hasUnsavedChanges(){return dirty.value||Boolean(impactDecision.value)||Boolean(pendingWrite.value)||Boolean(sharePanel.value?.hasUnsavedChanges())}
function canLeave(){
  if(operationBusy.value)return false
  if(sharePanel.value&&!sharePanel.value.canLeave())return false
  if(impactDecision.value)return confirm('修改影响还未确认，确定离开？本次检查不会创建新版本；离开会放弃本页未保存编辑。')
  if(pendingWrite.value?.result)return confirm('题目已保存，但最新状态尚未读回。离开后可重新打开核对。确定离开？')
  if(pendingWrite.value?.uncertain)return confirm('上一次保存的结果尚未确认，操作可能已经成功。离开会放弃本页的原标识重试入口，建议先重试核对。仍要离开？')
  return !dirty.value||confirm('题目内容还有未保存的修改，确定放弃这些修改？')
}
function showFailure(error:unknown,scope:Scope){
  if(error instanceof StaleLibraryResponse||!current(scope))return
  failure.value=error instanceof ApiError?error:new ApiError(0,'CLIENT_ERROR',error instanceof Error?error.message:'操作未完成，请重试。')
  if(failure.value.status===404&&(collection.value==='shared'||lockedQuestion.value)){
    const unavailable=selectedRoot.value?.id;clearSelection()
    if(unavailable)entries.value=entries.value.filter(entry=>entry.root.id!==unavailable)
  }
  if(failure.value.status===401)emit('unauthorized')
}
async function run(action:(scope:Scope)=>Promise<void>){
  if(operationBusy.value||!alive)return
  const scope=capture();busy.value=true;emitBusy();failure.value=null
  try{await action(scope);check(scope)}catch(error){showFailure(error,scope)}
  finally{if(current(scope)){busy.value=false;emitBusy()}}
}
function clearUsage(){usageRows.value=[];usageNext.value=null;usageLoaded.value=false}
function clearEditor(){impactDecision.value=null;impactBusy.value=false;editor.value=null;editBaseline.value='';editParentVersionId.value='';editExpectedHeadId.value='';pendingWrite.value=null;refreshNeeded.value=false;conflictNeedsReview.value=false;previewAnswer.value=null}
function clearSelection(){clearEditor();selectedRoot.value=null;history.value=[];historyOffset.value=0;historyMore.value=false;selectedVersionId.value='';clearUsage()}
function uniqueVersions(items:QuestionVersion[]){return [...new Map(items.map(item=>[item.id,item])).values()].sort((a,b)=>b.versionNo-a.versionNo)}
async function fetchList(offset:number,scope:Scope){
  const shared=collection.value==='shared',pageIndex=Math.floor(offset/20)
  const after=sharedCursors.value[pageIndex]
  const path=shared?`/shared-questions?limit=20&includeCurrentVersion=true${after?`&afterId=${encodeURIComponent(after)}`:''}`:`/questions?limit=20&offset=${offset}&includeCurrentVersion=true`
  const page=await request<{items:ListQuestion[];nextAfterId?:string|null}>(path);check(scope)
  if(shared){sharedCursors.value=sharedCursors.value.slice(0,pageIndex+1);if(page.nextAfterId)sharedCursors.value.push(page.nextAfterId)}
  const items=page.items.map(root=>{
    const value=root.currentVersion
    if(!value||value.id!==root.currentVersionId||value.questionId!==root.id||!value.content||typeof value.content.title!=='string'||!(value.content.kind in kindNames))throw new ApiError(0,'INVALID_QUESTION_SUMMARY','题目摘要未完整读取，请刷新列表。')
    return {root,current:value}
  });check(scope)
  return {items,more:shared?Boolean(page.nextAfterId):page.items.length===20&&offset+20<=100000}
}
async function readQuestion(id:string,preferred:string|undefined,scope:Scope){
  const root=await request<ReusableQuestion>(pathQuestion(id));check(scope)
  const page=await request<HistoryPage>(`${pathQuestion(id)}/versions?limit=30&offset=0`);check(scope)
  const versions=[...page.items]
  for(const wanted of [root.currentVersionId,preferred].filter((value):value is string=>Boolean(value))){
    if(!versions.some(item=>item.id===wanted)){
      const version=await request<QuestionVersion>(`${pathQuestion(id)}/versions/${encodeURIComponent(wanted)}`);check(scope);versions.push(version)
    }
  }
  return {root,versions:uniqueVersions(versions),offset:page.items.length,more:page.items.length===30}
}
function applyQuestion(data:Awaited<ReturnType<typeof readQuestion>>,preferred?:string){
  selectedRoot.value=data.root;history.value=data.versions;historyOffset.value=data.offset;historyMore.value=data.more
  selectedVersionId.value=preferred||data.root.currentVersionId;clearUsage();previewAnswer.value=null
}
async function changeCollection(next:'owned'|'shared'){
  if(lockedQuestion.value||collection.value===next||!canLeave())return
  collection.value=next;sharedCursors.value=[null];listOffset.value=0
  await loadQuestions(0,true)
}
async function loadQuestions(offset=0,leaveApproved=false){
  if(!leaveApproved&&!canLeave())return
  if(lockedQuestion.value&&props.questionId){await openQuestion(props.questionId,props.versionId);return}
  generation++;clearSelection();entries.value=[];listLoaded.value=false;note.value=''
  await run(async scope=>{const page=await fetchList(offset,scope);check(scope);entries.value=page.items;listOffset.value=offset;listMore.value=page.more;listLoaded.value=true})
}
async function openQuestion(id:string,preferred?:string){
  if(lockedQuestion.value&&id!==props.questionId)return
  if(!canLeave())return
  generation++;clearSelection();note.value=''
  await run(async scope=>{
    const data=await readQuestion(id,preferred,scope);check(scope);applyQuestion(data,preferred)
    if(lockedQuestion.value){const version=data.versions.find(item=>item.id===data.root.currentVersionId)!;entries.value=[{root:data.root,current:version}];listOffset.value=0;listMore.value=false;listLoaded.value=true}
  })
}
function startCreate(kind:Kind='single'){
  if(props.mode==='pick'||collection.value!=='owned'||!canLeave())return
  generation++;clearSelection();failure.value=null;note.value=''
  const question=newQuestion(kind);editor.value={kind,title:'',config:clone(question.config)};editBaseline.value=JSON.stringify(editor.value)
}
function startEdit(){
  if(!ownerSelection.value||!selectedVersion.value||!selectedRoot.value||!canLeave())return
  clearEditor();failure.value=null;note.value=''
  editor.value=clone(selectedVersion.value.content);editBaseline.value=JSON.stringify(editor.value)
  editParentVersionId.value=selectedVersion.value.id;editExpectedHeadId.value=selectedRoot.value.currentVersionId
}
function selectVersion(id:string){
  if(!history.value.some(item=>item.id===id)||!canLeave())return
  clearEditor();failure.value=null;note.value='';selectedVersionId.value=id;clearUsage()
}
function cancelEdit(){
  if(!canLeave())return
  const saved=pendingWrite.value?.result,uncertain=pendingWrite.value?.uncertain
  clearEditor();failure.value=null
  note.value=saved?'已退出编辑，保存已经完成。可以刷新题目列表核对。':uncertain?'已退出编辑。上次结果尚未确认，请刷新题目列表核对。':''
}
function changeKind(event:Event){
  if(!editor.value||editorLocked.value)return
  const kind=(event.target as HTMLSelectElement).value as Kind;editor.value.kind=kind;editor.value.config=clone(newQuestion(kind).config)
}
function setLimit(key:keyof Question['config'],event:Event){
  if(!editor.value||editorLocked.value)return
  const value=(event.target as HTMLInputElement).value;(editor.value.config as Record<string,unknown>)[key]=value===''?null:Number(value)
}
function addOption(){if(editor.value&&!editorLocked.value&&(editor.value.config.options?.length||0)<100)editor.value.config.options?.push({id:uid('o'),label:'新选项'})}
function removeOption(index:number){if(editor.value&&!editorLocked.value)editor.value.config.options?.splice(index,1)}
function focusIssue(issue:FieldError){
  const nodes=Array.from(libraryRoot.value?.querySelectorAll<HTMLElement>('[data-library-field]')||[])
  const node=nodes.find(item=>item.dataset.libraryField===issue.field)||nodes.find(item=>item.dataset.libraryField==='content.title')
  node?.scrollIntoView({block:'center'});node?.focus({preventScroll:true})
}
function contentBody():Record<string,unknown>|null{
  if(!editor.value)return null
  return editParentVersionId.value?{content:clone(editor.value),parentVersionId:editParentVersionId.value,expectedCurrentVersionId:editExpectedHeadId.value}:{content:clone(editor.value)}
}
function uncertainFailure(error:unknown){return !(error instanceof ApiError)||error.status===0||error.status>=500}
async function readSaved(operation:PendingWrite,scope:Scope){
  const saved=operation.result!
  const data=await readQuestion(saved.questionId,saved.id,scope);check(scope)
  const offset=operation.kind==='create'?0:listOffset.value
  const page=await fetchList(offset,scope);check(scope)
  clearEditor();applyQuestion(data,saved.id);entries.value=page.items;listOffset.value=offset;listMore.value=page.more;listLoaded.value=true
  note.value=`V${saved.versionNo}已保存。${operation.kind==='restore'?'原有问卷仍保留原版本。':'既有版本和已发布问卷保持原样。'}`
}
async function executeWrite(){
  if(!pendingWrite.value||busy.value)return
  await run(async scope=>{
    const operation=pendingWrite.value!
    if(!operation.result){
      try{
        const result=await post<QuestionVersion>(operation.path,operation.body,{'Idempotency-Key':operation.key});check(scope)
        operation.result=result;operation.uncertain=false;pendingWrite.value={...operation};note.value=`V${result.versionNo}已保存，正在读取最新状态。`
      }catch(error){
        if(current(scope)){operation.uncertain=uncertainFailure(error);pendingWrite.value={...operation};conflictNeedsReview.value=error instanceof ApiError&&['QUESTION_VERSION_CONFLICT','IDEMPOTENCY_CONFLICT'].includes(error.code)}
        throw error
      }
    }
    try{await readSaved(operation,scope)}catch(error){
      if(current(scope)){refreshNeeded.value=true;note.value=`V${operation.result!.versionNo}已保存，但读取最新状态失败。请重新读取；不会再次提交保存。`}
      throw error
    }
  })
}
function requestImpact(kind:'derive'|'restore',body:Record<string,unknown>){
  if(!ownerSelection.value||!selectedRoot.value||!selectedVersion.value||impactDecision.value||pendingWrite.value)return
  impactDecision.value={id:uid('impact_decision'),questionId:selectedRoot.value.id,
    sourceVersionId:kind==='restore'?String(body.targetVersionId):String(body.parentVersionId),
    sourceVersionLabel:versionLabel(kind==='restore'?String(body.targetVersionId):String(body.parentVersionId)),
    kind,path:`${pathQuestion(selectedRoot.value.id)}/${kind==='restore'?'restore':'versions'}`,body:clone(body),session:capture()}
  failure.value=null
}
function cancelImpact(){if(!impactBusy.value)impactDecision.value=null}
async function confirmImpact(approval:ImpactApproval){
  const decision=impactDecision.value
  if(!decision||operationBusy.value||!current(decision.session)||!ownerSelection.value||!selectedRoot.value||pendingWrite.value
    ||approval?.decisionId!==decision.id||approval.questionId!==decision.questionId||approval.sourceVersionId!==decision.sourceVersionId||!approval.checkedAt
    ||selectedRoot.value.id!==decision.questionId||selectedVersionId.value!==decision.sourceVersionId)return
  if(decision.kind==='derive'&&JSON.stringify(contentBody())!==JSON.stringify(decision.body)){
    impactDecision.value=null;failure.value=new ApiError(409,'IMPACT_CONTENT_CHANGED','本地内容已变化，请重新检查影响后保存。');return
  }
  impactDecision.value=null
  pendingWrite.value={kind:decision.kind,key:uid(decision.kind==='restore'?'question_restore':'question_save'),path:decision.path,body:clone(decision.body),uncertain:true}
  await executeWrite()
}
async function saveContent(){
  if(props.mode==='pick'||collection.value!=='owned'||operationBusy.value||impactDecision.value||!editor.value||conflictNeedsReview.value)return
  if(pendingWrite.value?.result){await refreshSaved();return}
  const body=contentBody()!
  if(pendingWrite.value&&JSON.stringify(body)!==JSON.stringify(pendingWrite.value.body)){
    if(pendingWrite.value.uncertain){failure.value=new ApiError(409,'UNCONFIRMED_WRITE','上一次保存结果尚未确认，请先重试原操作或明确放弃重试。');return}
    pendingWrite.value=null
  }
  if(editParentVersionId.value&&!pendingWrite.value){requestImpact('derive',body);return}
  pendingWrite.value ||= {kind:editParentVersionId.value?'derive':'create',key:uid('question_save'),path:editParentVersionId.value?`${pathQuestion(selectedRoot.value!.id)}/versions`:'/questions',body,uncertain:true}
  await executeWrite()
}
async function restoreVersion(){
  if(!ownerSelection.value||operationBusy.value||impactDecision.value||!selectedVersion.value||!selectedRoot.value||editor.value||conflictNeedsReview.value)return
  if(pendingWrite.value?.result){await refreshSaved();return}
  if(pendingWrite.value){await retryWrite();return}
  const target=selectedVersion.value
  requestImpact('restore',{targetVersionId:target.id,expectedCurrentVersionId:selectedRoot.value.currentVersionId})
}
async function retryWrite(){if(conflictNeedsReview.value)return;if(pendingWrite.value?.result)await refreshSaved();else await executeWrite()}
async function refreshSaved(){if(!pendingWrite.value?.result||busy.value)return;await executeWrite()}
async function reviewConflict(){
  if(operationBusy.value||!selectedRoot.value||!conflictNeedsReview.value||pendingWrite.value?.uncertain)return
  await run(async scope=>{
    const data=await readQuestion(selectedRoot.value!.id,selectedVersionId.value,scope);check(scope)
    selectedRoot.value=data.root;history.value=data.versions;historyOffset.value=data.offset;historyMore.value=data.more
    if(!confirm(`当前版本为V${data.root.lastVersionNo}。保留所选来源和本地内容，改用这个当前版本作为保存依据？确认后仍需再次点击保存或重试。`))return
    editExpectedHeadId.value=data.root.currentVersionId
    if(pendingWrite.value?.kind==='restore')pendingWrite.value={...pendingWrite.value,key:uid('question_restore'),body:{...pendingWrite.value.body,expectedCurrentVersionId:data.root.currentVersionId},uncertain:false}
    else pendingWrite.value=null
    conflictNeedsReview.value=false;failure.value=null;note.value='已核对当前版本。请再次点击保存或重试，提交新的操作。'
  })
}
function abandonPending(){
  if(operationBusy.value||!pendingWrite.value||pendingWrite.value.result)return
  if(!confirm(pendingWrite.value.uncertain?'操作可能已经保存成功。确定放弃原标识重试？之后请先核对题目列表，避免重复创建。':'确定放弃本次操作标识？本地编辑内容会保留。'))return
  const uncertain=pendingWrite.value.uncertain;pendingWrite.value=null;refreshNeeded.value=false;conflictNeedsReview.value=false;failure.value=null
  note.value=uncertain?'已放弃重试。请先刷新题目列表核对是否已保存。':'操作标识已放弃，可以继续修改内容。'
}
async function loadMoreHistory(){
  if(operationBusy.value||!selectedRoot.value||!historyMore.value||historyOffset.value>100000)return
  const id=selectedRoot.value.id,offset=historyOffset.value
  await run(async scope=>{const page=await request<HistoryPage>(`${pathQuestion(id)}/versions?limit=30&offset=${offset}`);check(scope);history.value=uniqueVersions([...history.value,...page.items]);historyOffset.value=offset+page.items.length;historyMore.value=page.items.length===30&&historyOffset.value<=100000})
}
async function loadUsage(append=false){
  if(operationBusy.value||!selectedRoot.value||append&&!usageNext.value)return
  const id=selectedRoot.value.id,scopeValue=usageScope.value,version=usageVersion.value==='selected'?selectedVersionId.value:''
  const params=new URLSearchParams({scope:scopeValue,limit:'20'});if(version)params.set('questionVersionId',version);if(append&&usageNext.value)params.set('afterId',usageNext.value)
  await run(async scope=>{const page=await request<{items:QuestionUsage[];nextAfterId:string|null}>(`${pathQuestion(id)}/usages?${params}`);check(scope);usageRows.value=append?[...usageRows.value,...page.items]:page.items;usageNext.value=page.nextAfterId;usageLoaded.value=true})
}
function chooseVersion(){if(props.mode!=='pick'||operationBusy.value||!selectedVersion.value)return;emit('choose',clone(selectedVersion.value))}
watch(editor,()=>{
  previewAnswer.value=null
  if(pendingWrite.value&&!pendingWrite.value.result&&!pendingWrite.value.uncertain&&!conflictNeedsReview.value&&pendingWrite.value.kind!=='restore'&&JSON.stringify(contentBody())!==JSON.stringify(pendingWrite.value.body))pendingWrite.value=null
  if(failure.value&&['INVALID_QUESTION_CONTENT','INVALID_REQUEST'].includes(failure.value.code))failure.value=null
},{deep:true,flush:'sync'})
watch(()=>[usageScope.value,usageVersion.value],clearUsage)
async function initialize(){if(lockedQuestion.value&&props.questionId)await openQuestion(props.questionId,props.versionId);else await loadQuestions()}
watch(()=>[props.mode,props.questionId,props.versionId],()=>{generation++;busy.value=false;sharingBusy.value=false;impactBusy.value=false;emitBusy();collection.value='owned';sharedCursors.value=[null];clearSelection();entries.value=[];listLoaded.value=false;void initialize()})
onMounted(()=>void initialize())
onUnmounted(()=>{alive=false;generation++})
defineExpose({canLeave,hasUnsavedChanges})
</script>

<template>
  <div ref="libraryRoot" class="question-library" :aria-busy="operationBusy">
    <div class="section-heading library-heading"><div><div class="eyebrow">{{mode==='pick'?'选择固定版本':collection==='owned'?'我的题目':'共享给我的'}}</div><h2>{{mode==='pick'?(lockedQuestion?'为这道题选择版本':'从已保存题目中选题'):collection==='shared'?'查看别人共享的题目':'题目管理'}}</h2><p class="hint">{{mode==='pick'?(destination==='bank'?'确认内容和版本后收录到题库。题库保留明确选择的一版；更新题库不会改变已有问卷。':'确认内容和版本后加入问卷，保存草稿时会核对使用权。题目以后更新时，本卷仍保留选中的版本。'):collection==='shared'?'可以查看历史与后续版本，在自己的问卷中选用；题目内容和共享权限由作者管理。':'保存修改或恢复旧内容时会新增版本，已选用的问卷保留原版。'}}</p></div><button v-if="mode==='manage'&&collection==='owned'" class="button primary" :disabled="operationBusy" @click="startCreate()">＋ 新建题目</button></div>
    <div v-if="!lockedQuestion" class="tabs library-collections" role="group" aria-label="题目来源"><button :class="{active:collection==='owned'}" :aria-pressed="collection==='owned'" :disabled="operationBusy" @click="changeCollection('owned')">我的题目</button><button :class="{active:collection==='shared'}" :aria-pressed="collection==='shared'" :disabled="operationBusy" @click="changeCollection('shared')">共享给我的</button></div>
    <p v-if="note" class="library-notice" role="status">{{note}}</p>
    <div v-if="failure" class="library-error" role="alert"><strong>{{failure.message}}</strong><p v-if="mode==='pick'&&lockedQuestion&&failure.status===404" class="hint">{{destination==='bank'?'这道题当前不可读取，可能已撤销共享。题库保留原整理记录，但不授予读取或换版权限；关闭窗口不会改变题库条目。':'这道题当前不可读取，可能已撤销共享。服务器已保存的固定引用可以保留；新选题或换版仍需作者授权。关闭选择窗口不会移除原草稿来源。'}}</p><ul v-if="failure.fieldErrors.length"><li v-for="(issue,index) in failure.fieldErrors" :key="index">{{issue.message}} <button v-if="editor" class="text-button small" @click="focusIssue(issue)">定位</button></li></ul></div>
    <div v-if="pendingWrite" class="panel pending-operation"><p>{{pendingWrite.result?'保存已经成功，重新读取不会再次创建版本。':pendingWrite.uncertain?'上次结果尚未确认，请使用原操作重试。确认前已暂停修改内容。':'本次操作尚未完成。可以按原标识重试，或修改内容后重新保存。'}}</p><div class="button-row"><button v-if="!conflictNeedsReview" class="button secondary" :disabled="operationBusy" @click="retryWrite">{{pendingWrite.result?'重新读取保存结果':'重试原操作'}}</button><button v-if="conflictNeedsReview&&selectedRoot" class="button secondary" :disabled="operationBusy" @click="reviewConflict">核对当前版本</button><button v-if="!pendingWrite.result" class="text-button" :disabled="operationBusy" @click="abandonPending">放弃本次重试</button></div></div>
    <QuestionImpactReview v-if="impactDecision" :key="impactDecision.id" :decision-id="impactDecision.id" :question-id="impactDecision.questionId" :source-version-id="impactDecision.sourceVersionId" :source-version-label="impactDecision.sourceVersionLabel" :operation="impactDecision.kind" @confirm="confirmImpact" @cancel="cancelImpact" @busy-change="onImpactBusy" @unauthorized="emit('unauthorized')"/>
    <div class="library-layout">
      <aside class="panel library-list"><div class="section-heading"><h3>{{lockedQuestion?'当前题目':collection==='shared'?'收到的共享题目':'已保存的题目'}}</h3><button class="text-button small" :disabled="operationBusy" @click="loadQuestions(listOffset)">刷新</button></div><p v-if="!listLoaded" class="hint">{{operationBusy?'正在读取题目…':'读取未完成，可以刷新重试。'}}</p><p v-else-if="!entries.length" class="hint">{{collection==='shared'?'当前没有有效的共享题目。请让作者按你的用户名共享，再刷新列表。':mode==='manage'?'还没有保存的题目，点击“新建题目”开始。':'还没有保存的题目，请先到题目管理页面创建。'}}</p><button v-for="entry in entries" :key="entry.root.id" :class="['library-entry',{active:selectedRoot?.id===entry.root.id}]" :disabled="operationBusy" @click="openQuestion(entry.root.id,lockedQuestion?versionId:undefined)"><strong>{{entry.current.content.title}}</strong><span>{{kindNames[entry.current.content.kind]}} · 当前V{{entry.current.versionNo}}</span><small>{{formatDate(entry.root.updatedAt)}}</small></button><div v-if="!lockedQuestion&&(listOffset||listMore)" class="library-pages"><button class="button secondary small" :disabled="operationBusy||listOffset===0" @click="loadQuestions(Math.max(0,listOffset-20))">上一页</button><span class="muted small">第{{Math.floor(listOffset/20)+1}}页</span><button class="button secondary small" :disabled="operationBusy||!listMore" @click="loadQuestions(listOffset+20)">下一页</button></div></aside>
      <div class="library-main">
        <section v-if="editor" class="panel library-editor"><div class="section-heading"><div><div class="eyebrow">{{editParentVersionId?`基于${versionLabel(editParentVersionId)}编辑`:'新建个人题目'}}</div><h3>{{editParentVersionId?'保存为新版本':'设置题目内容'}}</h3></div><span class="muted small">{{dirty?'有未保存修改':'内容未修改'}}</span></div><fieldset :disabled="editorLocked" class="library-fieldset"><label>题目名称<input v-model="editor.title" data-library-field="content.title" maxlength="500" placeholder="请输入题干"/></label><label>题型<select :value="editor.kind" data-library-field="content.kind" @change="changeKind"><option v-for="kind in (['single','multi','text','number'] as Kind[])" :key="kind" :value="kind">{{kindNames[kind]}}</option></select></label><template v-if="editor.kind==='single'||editor.kind==='multi'"><div class="field-heading">选项 <small>修改已有选项文案时保留其标识</small></div><div v-for="(option,index) in editor.config.options" :key="option.id" class="option-editor"><span>{{String.fromCharCode(65+index)}}</span><input v-model="option.label" :data-library-field="`content.config.options[${index}].label`" :aria-label="`选项${index+1}`" maxlength="500"/><button class="icon-button danger-text" :aria-label="`删除选项${index+1}`" @click="removeOption(index)">×</button></div><button class="text-button" :disabled="(editor.config.options?.length||0)>=100" data-library-field="content.config.options" @click="addOption">＋ 添加选项</button><div v-if="editor.kind==='multi'" class="limit-grid"><label>至少选择<input type="number" min="0" :value="editor.config.minSelected" data-library-field="content.config.minSelected" @input="setLimit('minSelected',$event)"/></label><label>最多选择<input type="number" min="0" :value="editor.config.maxSelected" data-library-field="content.config.maxSelected" placeholder="不限制" @input="setLimit('maxSelected',$event)"/></label></div></template><div v-if="editor.kind==='text'" class="limit-grid"><label>最少字数<input type="number" min="0" :value="editor.config.minLength" data-library-field="content.config.minLength" placeholder="不限制" @input="setLimit('minLength',$event)"/></label><label>最多字数<input type="number" min="0" :value="editor.config.maxLength" data-library-field="content.config.maxLength" placeholder="不限制" @input="setLimit('maxLength',$event)"/></label></div><template v-if="editor.kind==='number'"><div class="limit-grid"><label>最小值（含）<input type="number" :value="editor.config.min" data-library-field="content.config.min" placeholder="不限制" @input="setLimit('min',$event)"/></label><label>最大值（含）<input type="number" :value="editor.config.max" data-library-field="content.config.max" placeholder="不限制" @input="setLimit('max',$event)"/></label></div><label class="checkbox-line"><input v-model="editor.config.integerOnly" type="checkbox" data-library-field="content.config.integerOnly"/>只接受整数</label></template></fieldset><div class="button-row library-actions"><button class="button primary" :disabled="operationBusy||Boolean(impactDecision)||!editor.title.trim()||conflictNeedsReview" @click="saveContent">{{pendingWrite?.result?'重新读取保存结果':editParentVersionId?'检查影响并保存新版本':'保存题目'}}</button><button class="button secondary" :disabled="operationBusy" @click="cancelEdit">取消编辑</button></div><p class="hint">必答、卷内题目顺序和条件跳转在问卷编辑器中配置。</p></section>
        <section v-else-if="selectedVersion&&selectedRoot" class="panel library-version"><div class="section-heading"><div><div class="eyebrow">{{kindNames[selectedVersion.content.kind]}} · V{{selectedVersion.versionNo}} <span class="library-current" v-if="selectedVersion.id===selectedRoot.currentVersionId">当前版本</span></div><h3>{{selectedVersion.content.title}}</h3></div><button v-if="mode==='pick'" class="button primary" :disabled="operationBusy" @click="chooseVersion">选择V{{selectedVersion.versionNo}}</button></div><p class="hint">{{formatDate(selectedVersion.createdAt)}} · {{selectedVersion.parentVersionId?`内容来源：${versionLabel(selectedVersion.parentVersionId)}`:'初始版本'}}<template v-if="selectedVersion.operation==='restore'"> · 恢复记录，恢复前为{{versionLabel(selectedVersion.previousCurrentVersionId)}}</template></p><p v-if="mode==='pick'&&lockedQuestion" class="hint">{{destination==='bank'?'这里只更新同一道题的题库固定版本；已有问卷仍保留原版。':'这里只切换同一道题的版本；当前选中内容会在确认后应用到问卷草稿。'}}</p><div v-if="ownerSelection" class="button-row"><button class="button primary" :disabled="operationBusy||Boolean(pendingWrite)" @click="startEdit">基于此版编辑</button><button class="button secondary" :disabled="operationBusy||Boolean(pendingWrite)" @click="restoreVersion">恢复此版内容</button></div></section>
        <QuestionSharePanel v-if="ownerSelection&&!editor&&selectedRoot" :key="selectedRoot.id" ref="sharePanel" :question-id="selectedRoot.id" @busy-change="onShareBusy" @unauthorized="emit('unauthorized')"/>
        <section v-if="previewQuestion" class="panel library-preview"><div class="eyebrow">内容预览</div><h3>{{previewQuestion.title||'请输入题目名称'}}</h3><AnswerInput v-model="previewAnswer" :question="previewQuestion" :disabled="operationBusy"/><p class="hint">预览回答只保留在本页，不会提交答卷。</p></section>
        <div v-if="!editor&&!selectedVersion" class="panel empty-state"><h3>{{operationBusy?'正在读取…':'选择一道题目'}}</h3><p>从左侧打开题目，查看内容及已保存的版本。</p></div>
        <section v-if="selectedRoot" class="panel library-history"><div class="section-heading"><div><h3>版本历史</h3><p class="hint">版本号表示创建顺序，内容来源可以是更早的任意版本。</p></div></div><div class="library-history-list"><button v-for="version in history" :key="version.id" :disabled="operationBusy" :class="['library-history-item',{active:selectedVersionId===version.id}]" @click="selectVersion(version.id)"><strong>V{{version.versionNo}} <span v-if="version.id===selectedRoot.currentVersionId">当前</span></strong><span>{{version.content.title}}</span><small>{{version.operation==='restore'?'恢复版本':version.parentVersionId?'来源 '+versionLabel(version.parentVersionId):'初始版本'}} · {{formatDate(version.createdAt)}}</small></button></div><button v-if="historyMore" class="button secondary small" :disabled="operationBusy" @click="loadMoreHistory">继续读取历史</button></section>
        <section v-if="selectedRoot" class="panel library-usage"><button v-if="props.mode!=='pick'" class="button secondary" :disabled="operationBusy" @click="openStatistics">跨问卷统计</button><div class="section-heading"><div><h3>使用情况</h3><p class="hint">仅展示本人问卷。发布记录按快照列出，同一问卷可能出现多次。</p></div></div><fieldset :disabled="operationBusy" class="library-fieldset library-usage-controls"><label>范围<select v-model="usageScope"><option value="draft">当前草稿</option><option value="published">发布快照</option></select></label><label>题目版本<select v-model="usageVersion"><option value="all">整个题目系列</option><option value="selected">当前选中的V{{selectedVersion?.versionNo}}</option></select></label><button class="button secondary" @click="loadUsage()">查询使用情况</button></fieldset><p v-if="usageLoaded&&!usageRows.length" class="hint">这个范围内没有使用记录。</p><article v-for="row in usageRows" :key="row.surveyVersionId||row.surveyId" class="library-usage-row"><strong>{{row.surveyTitle}}</strong><span>{{usageScope==='draft'?`草稿修订${row.draftRevision}`:`问卷V${row.versionNo} · ${row.isCurrent?'当前发布版':'历史发布版'}`}} · {{({draft:'未发布',open:'收集中',closed:'已关闭'} as Record<string,string>)[row.surveyStatus]||row.surveyStatus}}</span><small>{{row.instances.length}}个题目实例：{{row.instances.map(item=>`${item.instanceId}（${versionLabel(item.questionVersionId)}）`).join('、')}}</small></article><button v-if="usageNext" class="button secondary small" :disabled="operationBusy" @click="loadUsage(true)">继续读取使用记录</button><p v-if="usageLoaded" class="hint">这是实时查询；问卷有新编辑时，可重新查询第一页。</p></section>
      </div>
    </div>
  </div>
</template>

<style scoped>
.library-collections{margin-bottom:20px}.library-heading{align-items:flex-start;margin-bottom:20px}.library-heading h2{margin:5px 0 8px;font-size:26px}.library-heading .hint{max-width:760px;margin:0}.library-layout{display:grid;grid-template-columns:270px minmax(0,1fr);gap:22px;align-items:start}.library-list{padding:20px;position:sticky;top:100px}.library-list h3{font-size:16px;margin:0}.library-entry{display:flex;flex-direction:column;gap:5px;text-align:left;width:100%;border:1px solid transparent;border-radius:10px;background:transparent;padding:14px 12px;margin-top:6px;color:inherit;cursor:pointer}.library-entry:hover,.library-entry.active{background:var(--blue-soft,#edf5ff);border-color:var(--blue,#2563eb)}.library-entry strong{line-height:1.5;overflow-wrap:anywhere}.library-entry span,.library-entry small{color:var(--muted,#64748b);font-size:12px}.library-pages{display:flex;align-items:center;justify-content:space-between;gap:7px;margin-top:18px}.library-main{min-width:0;display:grid;gap:20px}.library-main>.panel{margin:0}.library-fieldset{border:0;padding:0;margin:0;min-width:0}.library-fieldset>label{margin-bottom:16px}.library-fieldset:disabled{opacity:.75}.library-actions{margin-top:22px}.library-preview h3{font-size:20px;line-height:1.5;margin:10px 0 18px;overflow-wrap:anywhere}.library-preview>.hint{margin-bottom:0}.library-current{font-size:11px;border-radius:4px;background:#e5f3e8;color:#247545;padding:3px 7px;margin-left:8px}.library-notice{border:1px solid #bddac7;background:#f1f8f3;color:#245a38;padding:14px 18px;border-radius:10px}.library-error{border:1px solid #efc1c1;background:#fff5f5;color:#9b2c2c;padding:14px 18px;border-radius:10px;margin-bottom:18px}.library-error ul{padding-left:20px;margin-bottom:0}.pending-operation{margin-bottom:20px}.pending-operation p{margin-top:0}.library-history-list{display:grid;gap:8px;margin-bottom:14px}.library-history-item{display:grid;grid-template-columns:80px minmax(0,1fr);gap:5px 10px;border:1px solid #dfe5ed;border-radius:8px;background:#fff;padding:12px;text-align:left;color:inherit;cursor:pointer}.library-history-item.active{border-color:var(--blue,#2563eb);background:var(--blue-soft,#edf5ff)}.library-history-item>span{overflow-wrap:anywhere}.library-history-item strong span{font-size:11px;font-weight:400;color:#247545}.library-history-item small{grid-column:2;color:var(--muted,#64748b);line-height:1.5}.library-usage-controls{display:flex;gap:12px;align-items:flex-end;flex-wrap:wrap;margin-bottom:18px}.library-usage-controls>label{min-width:145px;margin:0}.library-usage-row{display:flex;flex-direction:column;gap:6px;padding:15px 0;border-top:1px solid #edf0f4}.library-usage-row>span,.library-usage-row>small{font-size:12px;color:var(--muted,#64748b);overflow-wrap:anywhere}.question-library button:disabled{cursor:default}.library-usage-row strong{overflow-wrap:anywhere}@media(max-width:900px){.library-layout{grid-template-columns:1fr}.library-list{position:static}.library-heading{gap:12px}.library-heading h2{font-size:22px}.library-history-item{grid-template-columns:65px minmax(0,1fr)}}
</style>
