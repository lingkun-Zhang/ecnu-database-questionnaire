<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import AnswerInput from './AnswerInput.vue'
import QuestionLibrary from './QuestionLibrary.vue'
import { ApiError, getToken, request } from '../api'
import { clone, kindNames, uid } from '../types'
import type { Answer, Question, QuestionVersion } from '../types'

const props=withDefaults(defineProps<{mode?:'manage'|'pick';questionId?:string}>(),{mode:'manage'})
const emit=defineEmits<{choose:[QuestionVersion];'busy-change':[boolean];unauthorized:[]}>()
interface Bank {id:string;name:string;description:string;archived:boolean;revision:number;createdAt:string;updatedAt:string}
interface BankItem {id:string;questionId:string;questionVersionId:string;createdAt:string;updatedAt:string;available:boolean;version?:QuestionVersion;currentVersionId?:string}
interface BankPage {items:Bank[];nextAfterId:string|null}
interface MemberPage {bank:Bank;items:BankItem[];nextAfterId:string|null}
interface Scope {token:string;generation:number;bankId:string|null}
interface PendingWrite {kind:'create'|'metadata'|'set'|'remove';bankId:string|null;path:string;method:'POST'|'PUT'|'DELETE';body?:Readonly<Record<string,unknown>>;key?:string;phase:'uncertain'|'conflict'|'ready'|'saved';result?:Bank;label:string;targetQuestionId?:string;targetVersionId?:string;acknowledgedGoal?:boolean}
interface Confirmation {kind:'archive'|'remove'|'abandon';scope:Scope;label:string;operation?:PendingWrite}
interface Picker {key:string;bankId:string;revision:number;questionId?:string;versionId?:string;title:string;scope:Scope;onBusy:(value:boolean)=>void;onChoose:(version:QuestionVersion)=>void;onUnauthorized:()=>void}

const busy=ref(false),pickerBusy=ref(false),failure=ref<ApiError|null>(null),note=ref('')
const banks=ref<Bank[]>([]),bankAfterId=ref<string|null>(null),bankPrevious=ref<Array<string|null>>([]),bankNext=ref<string|null>(null),banksLoaded=ref(false)
const archivedFilter=ref(false),activeBankId=ref<string|null>(null),selectedBank=ref<Bank|null>(null)
const members=ref<BankItem[]>([]),memberAfterId=ref<string|null>(null),memberPrevious=ref<Array<string|null>>([]),memberNext=ref<string|null>(null),membersLoaded=ref(false),selectedItemId=ref('')
const formMode=ref<'create'|'edit'|null>(null),formName=ref(''),formDescription=ref(''),formBaseline=ref('')
const pendingWrite=ref<PendingWrite|null>(null),confirmation=ref<Confirmation|null>(null),confirmationCancel=ref<HTMLButtonElement|null>(null)
const conflictBank=ref<Bank|null>(null),conflictItem=ref<BankItem|null>(null),conflictChecked=ref(false),conflictNext=ref<string|null>(null)
const picker=ref<Picker|null>(null),pickerRef=ref<{canLeave:()=>boolean;hasUnsavedChanges:()=>boolean}|null>(null)
const previewAnswer=ref<Answer>(null),previewId=uid('bank_preview')
let generation=0,alive=true,ownerToken=getToken()
class StaleBankResponse extends Error {}
const operationBusy=computed(()=>busy.value||pickerBusy.value)
const locked=computed(()=>operationBusy.value||Boolean(pendingWrite.value||confirmation.value||picker.value))
const formDirty=computed(()=>formMode.value!==null&&JSON.stringify({name:formName.value,description:formDescription.value})!==formBaseline.value)
const selectedItem=computed(()=>members.value.find(item=>item.id===selectedItemId.value)||null)
const visibleMembers=computed(()=>props.mode==='manage'?members.value:members.value.filter(item=>item.available&&(!props.questionId||item.questionId===props.questionId)))
const canManageMembers=computed(()=>props.mode==='manage'&&Boolean(selectedBank.value&&!selectedBank.value.archived)&&!locked.value&&!formMode.value)
const previewQuestion=computed<Question|null>(()=>{
  const version=selectedItem.value?.available?selectedItem.value.version:undefined
  return version?{id:previewId,...clone(version.content),required:false,defaultTarget:'END',rules:[]}:null
})
function bankPath(id:string){return `/question-banks/${encodeURIComponent(id)}`}
function membersPath(id:string){return `${bankPath(id)}/questions`}
function capture():Scope{return {token:getToken(),generation,bankId:activeBankId.value}}
function current(scope:Scope){return alive&&scope.token===ownerToken&&scope.token===getToken()&&scope.generation===generation&&(scope.bankId===null||scope.bankId===activeBankId.value)}
function check(scope:Scope){if(!current(scope))throw new StaleBankResponse()}
function emitBusy(){emit('busy-change',operationBusy.value)}
function hasUnsavedChanges(){return formDirty.value||Boolean(pendingWrite.value||confirmation.value||picker.value)||Boolean(pickerRef.value?.hasUnsavedChanges())}
function canLeave(){
  if(operationBusy.value||confirmation.value||picker.value)return false
  const operation=pendingWrite.value
  if(operation?.phase==='uncertain')return confirm('题库操作的结果尚未确认，可能已经保存。离开会失去本页的原请求重试入口，仍要离开？')
  if(operation?.phase==='saved')return confirm('题库操作已保存，但最新列表尚未读回。离开后可以重新打开核对，确定离开？')
  if(operation)return confirm('题库操作还在等待核对或提交，确定放弃本页这次操作？')
  return !formDirty.value||confirm('题库信息还有未保存的修改，确定放弃这些修改？')
}
function showFailure(error:unknown,scope:Scope){
  if(error instanceof StaleBankResponse||!current(scope))return
  failure.value=error instanceof ApiError?error:new ApiError(0,'CLIENT_ERROR',error instanceof Error?error.message:'操作未完成，请重试。')
  if(failure.value.status===401)emit('unauthorized')
}
async function run(action:(scope:Scope)=>Promise<void>){
  if(operationBusy.value||!alive||getToken()!==ownerToken)return
  const scope=capture();busy.value=true;emitBusy();failure.value=null
  try{await action(scope);check(scope)}catch(error){showFailure(error,scope)}
  finally{if(current(scope)){busy.value=false;emitBusy()}}
}
function clearConflict(){conflictBank.value=null;conflictItem.value=null;conflictChecked.value=false;conflictNext.value=null}
function clearForm(){formMode.value=null;formName.value='';formDescription.value='';formBaseline.value=''}
function clearSelection(){activeBankId.value=null;selectedBank.value=null;members.value=[];memberAfterId.value=null;memberPrevious.value=[];memberNext.value=null;membersLoaded.value=false;selectedItemId.value='';previewAnswer.value=null;clearForm();pendingWrite.value=null;confirmation.value=null;clearConflict();picker.value=null;pickerBusy.value=false}
function cursorParams(afterId:string|null){const params=new URLSearchParams({limit:'20'});if(afterId)params.set('afterId',afterId);return params}
async function readBanks(afterId:string|null,archived:boolean,scope:Scope){const params=cursorParams(afterId);params.set('archived',String(archived));const page=await request<BankPage>(`/question-banks?${params}`);check(scope);return page}
function applyBanks(page:BankPage,afterId:string|null){banks.value=page.items;bankAfterId.value=afterId;bankNext.value=page.nextAfterId;banksLoaded.value=true}
async function readMembers(bankId:string,afterId:string|null,scope:Scope){const page=await request<MemberPage>(`${membersPath(bankId)}?${cursorParams(afterId)}`);check(scope);if(page.bank.id!==bankId)throw new ApiError(409,'BANK_RESPONSE_CHANGED','题库返回内容与本次读取不一致，请重新打开题库。');return page}
function applyMembers(page:MemberPage,afterId:string|null){selectedBank.value=page.bank;activeBankId.value=page.bank.id;members.value=page.items;memberAfterId.value=afterId;memberNext.value=page.nextAfterId;membersLoaded.value=true;selectedItemId.value='';previewAnswer.value=null}
async function loadBanks(leaveApproved=false){
  if(locked.value||!leaveApproved&&!canLeave())return
  const after=bankAfterId.value;generation++;clearSelection();note.value='';banks.value=[];banksLoaded.value=false
  await run(async scope=>{const page=await readBanks(after,archivedFilter.value,scope);applyBanks(page,after)})
}
async function changeArchiveFilter(value:boolean){
  if(props.mode==='pick'||locked.value||value===archivedFilter.value||!canLeave())return
  archivedFilter.value=value;bankAfterId.value=null;bankPrevious.value=[];await loadBanks(true)
}
async function loadNextBanks(){
  if(locked.value||!bankNext.value||!canLeave())return
  const next=bankNext.value,previous=bankAfterId.value;generation++;clearSelection();note.value=''
  await run(async scope=>{const page=await readBanks(next,archivedFilter.value,scope);bankPrevious.value=[...bankPrevious.value,previous];applyBanks(page,next)})
}
async function loadPreviousBanks(){
  if(locked.value||!bankPrevious.value.length||!canLeave())return
  const previous=bankPrevious.value.at(-1)!;generation++;clearSelection();note.value=''
  await run(async scope=>{const page=await readBanks(previous,archivedFilter.value,scope);bankPrevious.value=bankPrevious.value.slice(0,-1);applyBanks(page,previous)})
}
async function openBank(value:Bank|string){
  if(locked.value||!canLeave())return
  const id=typeof value==='string'?value:value.id
  generation++;clearSelection();activeBankId.value=id;note.value=''
  await run(async scope=>{const page=await readMembers(id,null,scope);if(props.mode==='pick'&&page.bank.archived)throw new ApiError(409,'BANK_ARCHIVED','这个题库已归档，请选择未归档题库。');applyMembers(page,null)})
}
async function loadMembers(){
  if(locked.value||formMode.value||!selectedBank.value)return
  const id=selectedBank.value.id,after=memberAfterId.value;members.value=[];membersLoaded.value=false;selectedItemId.value='';previewAnswer.value=null
  await run(async scope=>{const page=await readMembers(id,after,scope);applyMembers(page,after)})
}
async function loadNextMembers(){
  if(locked.value||formMode.value||!selectedBank.value||!memberNext.value)return
  const id=selectedBank.value.id,next=memberNext.value,previous=memberAfterId.value
  await run(async scope=>{const page=await readMembers(id,next,scope);memberPrevious.value=[...memberPrevious.value,previous];applyMembers(page,next)})
}
async function loadPreviousMembers(){
  if(locked.value||formMode.value||!selectedBank.value||!memberPrevious.value.length)return
  const id=selectedBank.value.id,previous=memberPrevious.value.at(-1)!
  await run(async scope=>{const page=await readMembers(id,previous,scope);memberPrevious.value=memberPrevious.value.slice(0,-1);applyMembers(page,previous)})
}
function startCreate(){
  if(props.mode!=='manage'||locked.value||!canLeave())return
  generation++;clearSelection();formMode.value='create';formBaseline.value=JSON.stringify({name:'',description:''});failure.value=null;note.value=''
}
function startEdit(){
  if(props.mode!=='manage'||locked.value||!selectedBank.value||!canLeave())return
  formMode.value='edit';formName.value=selectedBank.value.name;formDescription.value=selectedBank.value.description;formBaseline.value=JSON.stringify({name:formName.value,description:formDescription.value});failure.value=null;note.value=''
}
function cancelForm(){if(locked.value||!canLeave())return;clearForm();failure.value=null;note.value=''}
function freezeBody(body:Record<string,unknown>){return Object.freeze(clone(body))}
function uncertain(error:unknown){return !(error instanceof ApiError)||error.status===0||error.status>=500}
async function saveForm(){
  if(props.mode!=='manage'||locked.value||!formMode.value)return
  const name=formName.value.trim(),description=formDescription.value
  if(!name||name.length>120||description.length>2000){failure.value=new ApiError(422,'INVALID_REQUEST','请输入1—120字的题库名称，说明最多2000字。');return}
  if(formMode.value==='create')pendingWrite.value={kind:'create',bankId:null,path:'/question-banks',method:'POST',key:uid('bank_create'),body:freezeBody({name,description}),phase:'uncertain',label:`创建题库「${name}」`}
  else{
    const bank=selectedBank.value;if(!bank)return
    pendingWrite.value={kind:'metadata',bankId:bank.id,path:bankPath(bank.id),method:'PUT',body:freezeBody({name,description,archived:bank.archived,expectedRevision:bank.revision}),phase:'uncertain',label:`保存「${name}」的信息`}
  }
  await executeWrite()
}
function requestArchive(){
  const bank=selectedBank.value;if(props.mode!=='manage'||locked.value||formMode.value||!bank)return
  const operation:PendingWrite={kind:'metadata',bankId:bank.id,path:bankPath(bank.id),method:'PUT',body:freezeBody({name:bank.name,description:bank.description,archived:!bank.archived,expectedRevision:bank.revision}),phase:'uncertain',label:`${bank.archived?'恢复':'归档'}题库「${bank.name}」`}
  confirmation.value={kind:'archive',scope:capture(),label:operation.label,operation};failure.value=null;note.value='';focusConfirmation()
}
function requestRemove(row:BankItem){
  const bank=selectedBank.value;if(!canManageMembers.value||!bank||!members.value.some(item=>item.id===row.id&&item.questionId===row.questionId&&item.questionVersionId===row.questionVersionId))return
  const operation:PendingWrite={kind:'remove',bankId:bank.id,path:`${membersPath(bank.id)}/${encodeURIComponent(row.questionId)}?expectedRevision=${bank.revision}`,method:'DELETE',phase:'uncertain',label:`从「${bank.name}」移除「${row.version?.content.title||'当前不可用题目'}」`,targetQuestionId:row.questionId}
  confirmation.value={kind:'remove',scope:capture(),label:operation.label,operation};failure.value=null;note.value='';focusConfirmation()
}
function focusConfirmation(){void nextTick(()=>{const value=confirmation.value;if(value&&current(value.scope))confirmationCancel.value?.focus()})}
async function confirmAction(){
  const decision=confirmation.value;if(!decision||operationBusy.value||!current(decision.scope))return
  if(decision.kind==='abandon'){
    pendingWrite.value=null;confirmation.value=null;clearConflict();failure.value=null;note.value='已结束本次请求。请先重新读取题库，核对上次是否已经保存，再决定后续操作。';return
  }
  if(props.mode!=='manage'||pendingWrite.value||!decision.operation||decision.operation.bankId!==selectedBank.value?.id)return
  if(decision.kind==='remove'&&selectedBank.value.archived)return
  pendingWrite.value=decision.operation;confirmation.value=null;await executeWrite()
}
function cancelAction(){const decision=confirmation.value;if(!decision||operationBusy.value||!current(decision.scope))return;confirmation.value=null;note.value='已取消，本页没有提交这次修改。'}
async function readSaved(operation:PendingWrite,scope:Scope){
  const id=operation.result!.id
  const page=await readMembers(id,null,scope)
  const bankPage=await readBanks(null,page.bank.archived,scope);check(scope)
  archivedFilter.value=page.bank.archived;applyBanks(bankPage,null);bankPrevious.value=[]
  applyMembers(page,null);memberPrevious.value=[];clearForm();pendingWrite.value=null;clearConflict()
  note.value=operation.acknowledgedGoal?'当前题库已符合原决定，已结束核对，没有再次提交修改。':`${operation.label}的操作已保存，题库已重新读取。已有问卷保留原固定版本。`
}
async function executeWrite(){
  const initial=pendingWrite.value
  if(operationBusy.value||!initial||initial.phase==='conflict'||props.mode!=='manage'||getToken()!==ownerToken||initial.bankId!==null&&initial.bankId!==activeBankId.value)return
  await run(async scope=>{
    const operation=pendingWrite.value!
    if(operation.phase!=='saved'){
      try{
        const result=await request<Bank>(operation.path,{method:operation.method,...(operation.body?{body:JSON.stringify(operation.body)}:{}),...(operation.key?{headers:{'Idempotency-Key':operation.key}}:{})});check(scope)
        if(operation.bankId!==null&&result.id!==operation.bankId)throw new ApiError(409,'BANK_RESPONSE_CHANGED','保存返回的题库与本次请求不一致，请重新核对。')
        operation.phase='saved';operation.result=result;pendingWrite.value={...operation};note.value='题库操作已保存，正在重新读取结果。'
      }catch(error){
        if(current(scope)){
          if(error instanceof ApiError&&error.status===409){operation.phase='conflict';pendingWrite.value={...operation};clearConflict()}
          else if(uncertain(error)){operation.phase='uncertain';pendingWrite.value={...operation}}
          else pendingWrite.value=null
        }
        throw error
      }
    }
    try{await readSaved(operation,scope)}catch(error){if(current(scope))note.value=operation.acknowledgedGoal?'当前状态已核对，但最新列表尚未读回。重新读取只发送查询，不会提交修改。':'操作已保存，但最新题库尚未读回。重新读取只发送查询，不会重复提交修改。';throw error}
  })
}
async function retryWrite(){if(pendingWrite.value?.phase==='uncertain')await executeWrite()}
async function refreshSaved(){if(pendingWrite.value?.phase==='saved')await executeWrite()}
async function reviewConflict(){
  const operation=pendingWrite.value;if(operationBusy.value||operation?.phase!=='conflict'||!operation.bankId)return
  clearConflict()
  await run(async scope=>{
    const page=await readMembers(operation.bankId!,null,scope);check(scope)
    conflictBank.value=page.bank;applyConflictPage(page,operation)
    note.value='已读取题库当前状态。请核对原决定与当前状态；确认后仍需单独提交，不会自动修改。'
  })
}
function applyConflictPage(page:MemberPage,operation:PendingWrite){
  const found=operation.targetQuestionId?page.items.find(row=>row.questionId===operation.targetQuestionId):undefined
  if(found)conflictItem.value=found
  conflictChecked.value=operation.kind==='metadata'||Boolean(found)||!page.nextAfterId
  conflictNext.value=conflictChecked.value?null:page.nextAfterId
}
async function continueConflictReview(){
  const operation=pendingWrite.value,bank=conflictBank.value,next=conflictNext.value
  if(operationBusy.value||operation?.phase!=='conflict'||!bank||!next)return
  await run(async scope=>{
    const page=await readMembers(bank.id,next,scope)
    if(page.bank.revision!==bank.revision){clearConflict();throw new ApiError(409,'BANK_CONFLICT','核对期间题库再次变化，请重新读取第一页。')}
    applyConflictPage(page,operation)
  })
}
async function confirmConflict(){
  const operation=pendingWrite.value,bank=conflictBank.value
  if(operationBusy.value||operation?.phase!=='conflict'||!bank||!conflictChecked.value||bank.id!==operation.bankId||getToken()!==ownerToken)return
  if((operation.kind==='set'||operation.kind==='remove')&&bank.archived){failure.value=new ApiError(409,'BANK_ARCHIVED','题库目前已归档，不能调整成员。请结束本次请求后再决定是否恢复题库。');return}
  if(operation.kind==='set'&&conflictItem.value&&!conflictItem.value.available){failure.value=new ApiError(404,'NOT_FOUND','这道题目前不可用，不能重新提交收录或换版。');return}
  const already=operation.kind==='remove'&&!conflictItem.value||operation.kind==='set'&&conflictItem.value?.questionVersionId===operation.targetVersionId||operation.kind==='metadata'&&operation.body?.name===bank.name&&operation.body?.description===bank.description&&operation.body?.archived===bank.archived
  if(already){pendingWrite.value={...operation,phase:'saved',result:bank,acknowledgedGoal:true};clearConflict();await executeWrite();return}
  const revised:PendingWrite={...operation,phase:'ready',result:undefined}
  if(operation.method==='DELETE')revised.path=`${membersPath(bank.id)}/${encodeURIComponent(operation.targetQuestionId!)}?expectedRevision=${bank.revision}`
  else revised.body=freezeBody({...operation.body,expectedRevision:bank.revision})
  pendingWrite.value=revised;clearConflict();failure.value=null;note.value='已按刚读取的题库状态重新确认。尚未提交，请再次点击“提交已确认操作”。'
}
async function submitReviewedWrite(){if(pendingWrite.value?.phase==='ready')await executeWrite()}
function abandonPending(){if(operationBusy.value||!pendingWrite.value||confirmation.value||picker.value)return;confirmation.value={kind:'abandon',scope:capture(),label:'结束本次题库请求'};focusConfirmation()}
function onPickerBusy(value:boolean,key=picker.value?.key){if(!picker.value||picker.value.key!==key||!current(picker.value.scope))return;pickerBusy.value=value;emitBusy()}
function makePicker(questionId?:string,versionId?:string){
  const bank=selectedBank.value!;const key=uid('bank_picker'),scope=capture()
  picker.value={key,bankId:bank.id,revision:bank.revision,questionId,versionId,title:questionId?'更新题库所用版本':`收录到「${bank.name}」`,scope,onBusy:value=>onPickerBusy(value,key),onChoose:version=>void acceptPickerVersion(version,key),onUnauthorized:()=>{if(picker.value?.key===key&&current(scope))emit('unauthorized')}}
}
function openAdd(){if(!canManageMembers.value)return;failure.value=null;note.value='';makePicker()}
function openUpdate(row:BankItem){
  const stored=members.value.find(item=>item.id===row.id&&item.questionId===row.questionId&&item.questionVersionId===row.questionVersionId)
  if(!canManageMembers.value||!stored?.available||!stored.version)return
  failure.value=null;note.value='';makePicker(stored.questionId,stored.questionVersionId)
}
function closePicker(){if(operationBusy.value||!picker.value||pickerRef.value?.canLeave()===false)return;picker.value=null;pickerBusy.value=false;emitBusy()}
async function acceptPickerVersion(version:QuestionVersion,key=picker.value?.key){
  const decision=picker.value,bank=selectedBank.value
  if(props.mode!=='manage'||operationBusy.value||pendingWrite.value||confirmation.value||!decision||decision.key!==key||!current(decision.scope)||!bank||bank.id!==decision.bankId||bank.archived)return
  if(decision.questionId&&version.questionId!==decision.questionId){failure.value=new ApiError(422,'WRONG_QUESTION','这里只能更新原题目系列的版本。');return}
  pendingWrite.value={kind:'set',bankId:decision.bankId,path:`${membersPath(decision.bankId)}/${encodeURIComponent(version.questionId)}`,method:'PUT',body:freezeBody({questionVersionId:version.id,expectedRevision:decision.revision}),phase:'uncertain',label:`${decision.questionId?'更新':'收录'}「${version.content.title}」V${version.versionNo}`,targetQuestionId:version.questionId,targetVersionId:version.id}
  picker.value=null;pickerBusy.value=false;emitBusy();await executeWrite()
}
function selectItem(item:BankItem){
  const stored=members.value.find(row=>row.id===item.id&&row.questionId===item.questionId&&row.questionVersionId===item.questionVersionId)
  if(locked.value||formMode.value||!stored)return
  if(props.mode==='pick'&&(!stored.available||props.questionId&&stored.questionId!==props.questionId))return
  selectedItemId.value=item.id;previewAnswer.value=null;failure.value=null
}
function invalidateItem(id:string){members.value=members.value.map(item=>item.id===id?{id:item.id,questionId:item.questionId,questionVersionId:item.questionVersionId,createdAt:item.createdAt,updatedAt:item.updatedAt,available:false}:item);if(selectedItemId.value===id)selectedItemId.value='';previewAnswer.value=null}
async function chooseItem(){
  const item=selectedItem.value,bank=selectedBank.value
  if(props.mode!=='pick'||locked.value||!item?.available||!item.version||!bank||bank.archived||props.questionId&&item.questionId!==props.questionId)return
  if(item.version.id!==item.questionVersionId||item.version.questionId!==item.questionId)return
  let chosen:QuestionVersion|null=null,scopeUsed:Scope|null=null
  await run(async scope=>{
    scopeUsed=scope
    try{
      const latest=await request<Bank>(bankPath(bank.id));check(scope)
      if(latest.id!==bank.id||latest.archived||latest.revision!==bank.revision){members.value=[];membersLoaded.value=false;selectedItemId.value='';throw new ApiError(409,'BANK_SELECTION_CHANGED','题库已变化，请刷新成员后重新选择固定版本。')}
      const version=await request<QuestionVersion>(`/questions/${encodeURIComponent(item.questionId)}/versions/${encodeURIComponent(item.questionVersionId)}`);check(scope)
      if(version.id!==item.questionVersionId||version.questionId!==item.questionId)throw new ApiError(409,'BANK_SELECTION_CHANGED','题目版本与本次选择不一致，请重新读取。')
      chosen=clone(version)
    }catch(error){if(current(scope)&&error instanceof ApiError&&error.status===404)invalidateItem(item.id);throw error}
  })
  // App rejects a choice while its global question-busy flag is set. Emit only
  // after run() has completed its finally and released that flag.
  if(chosen&&scopeUsed&&current(scopeUsed)&&selectedItemId.value===item.id&&!operationBusy.value)emit('choose',chosen)
}
watch(selectedItemId,()=>{previewAnswer.value=null})
function reset(){generation++;ownerToken=getToken();busy.value=false;pickerBusy.value=false;clearSelection();banks.value=[];bankAfterId.value=null;bankPrevious.value=[];bankNext.value=null;banksLoaded.value=false;archivedFilter.value=false;failure.value=null;note.value='';emitBusy()}
watch(()=>[props.mode,props.questionId],()=>{reset();void loadBanks()})
onMounted(()=>void loadBanks())
onUnmounted(()=>{alive=false;generation++;confirmation.value=null;picker.value=null})
defineExpose({canLeave,hasUnsavedChanges})
</script>

<template>
  <div class="question-banks" :aria-busy="operationBusy">
    <div class="section-heading banks-heading"><div><div class="eyebrow">{{mode==='pick'?'选择固定版本':'整理常用题目'}}</div><h2>{{mode==='pick'?'从题库选题':'我的题库'}}</h2><p class="hint">{{mode==='pick'?'选择题库中明确保存的一版；加入后仍需保存问卷草稿。':'题库用于整理本人及有效共享题目。更新题库版本不会自动替换已有问卷。'}}</p></div><button v-if="mode==='manage'" class="button primary" :disabled="locked" @click="startCreate">新建题库</button></div>
    <div v-if="mode==='manage'" class="segmented banks-filters"><button :class="{active:!archivedFilter}" :disabled="locked" @click="changeArchiveFilter(false)">使用中的题库</button><button :class="{active:archivedFilter}" :disabled="locked" @click="changeArchiveFilter(true)">已归档题库</button></div>
    <p v-if="mode==='pick'&&questionId" class="banks-info">这里只能选择当前题目系列。其他题库成员不会替换这道题。</p>
    <p v-if="note" class="banks-notice" role="status">{{note}}</p>
    <div v-if="failure" class="banks-error" role="alert"><strong>{{failure.message}}</strong><p v-for="(issue,index) in failure.fieldErrors" :key="index">{{issue.message}}</p></div>
    <div v-if="confirmation" class="banks-confirm" role="alertdialog" aria-labelledby="bank-confirm-title" @keydown.esc.prevent="cancelAction"><h3 id="bank-confirm-title">{{confirmation.label}}？</h3><p v-if="confirmation.kind==='archive'">{{confirmation.operation?.body?.archived?'归档后保留题目整理记录，可以查看和恢复，但暂不能调整成员。':'恢复后可以继续收录、换版或移除成员。'}}已有问卷和独立题目不会因此改变。</p><p v-else-if="confirmation.kind==='remove'">只移除这条题库整理关系，不删除原题、已发布问卷或答卷。</p><p v-else>{{pendingWrite?.phase==='uncertain'?'上次操作可能已经保存，结束后会失去原请求重试入口。请先核对服务器状态，避免重复创建。':pendingWrite?.phase==='saved'?'操作已经保存，结束后仍可重新打开题库查看。':'原决定不会再次提交，表单中的本地内容会保留。'}}</p><div class="button-row"><button class="button primary" :disabled="operationBusy" @click="confirmAction">{{confirmation.kind==='abandon'?'确认结束请求':'确认操作'}}</button><button ref="confirmationCancel" class="button secondary" :disabled="operationBusy" @click="cancelAction">取消操作</button></div></div>
    <section v-if="pendingWrite" class="panel banks-pending"><h3>待确认操作</h3><p>{{pendingWrite.label}}</p><p v-if="pendingWrite.phase==='uncertain'">结果尚未确认。请按原请求重试，确认前暂停新的题库决定。</p><p v-else-if="pendingWrite.phase==='saved'">操作已保存。重新读取只刷新结果，不会重复提交修改。</p><p v-else-if="pendingWrite.phase==='ready'">已核对当前修订，尚未提交。请再次点击下方按钮。</p><p v-else>题库状态已变化。先读取当前状态，再明确确认；系统不会自动套用新修订重试。</p><div v-if="pendingWrite.phase==='conflict'&&conflictBank" class="banks-conflict-state"><strong>服务器当前：{{conflictBank.name}} · 修订{{conflictBank.revision}} · {{conflictBank.archived?'已归档':'使用中'}}</strong><p class="hint">{{conflictBank.description||'无题库说明'}}</p><p v-if="pendingWrite.kind==='metadata'">原决定：名称「{{pendingWrite.body?.name}}」，说明「{{pendingWrite.body?.description||'无'}}」，{{pendingWrite.body?.archived?'归档':'保持使用中'}}。</p><p v-else-if="conflictItem">当前条目：{{conflictItem.available?`${conflictItem.version?.content.title||'题目'} · V${conflictItem.version?.versionNo}`:'题目当前不可用'}}<template v-if="pendingWrite.kind==='set'">；原决定仍为所选版本，不会自动改选最新版。</template></p><p v-else>{{conflictChecked?'当前题库中没有这道题。':'当前页未找到原题目，需继续读取后续成员再核对。'}}</p></div><div class="button-row"><button v-if="pendingWrite.phase==='uncertain'" class="button secondary" :disabled="operationBusy||!!confirmation" @click="retryWrite">原样重试题库操作</button><button v-else-if="pendingWrite.phase==='saved'" class="button secondary" :disabled="operationBusy||!!confirmation" @click="refreshSaved">重新读取题库结果</button><button v-else-if="pendingWrite.phase==='ready'" class="button primary" :disabled="operationBusy||!!confirmation" @click="submitReviewedWrite">提交已确认操作</button><template v-else><button class="button secondary" :disabled="operationBusy||!!confirmation" @click="reviewConflict">重新读取并核对</button><button v-if="conflictNext" class="button secondary" :disabled="operationBusy||!!confirmation" @click="continueConflictReview">继续核对后续成员</button><button v-if="conflictChecked" class="button primary" :disabled="operationBusy||!!confirmation" @click="confirmConflict">确认按当前状态处理</button></template><button class="text-button" :disabled="operationBusy||!!confirmation" @click="abandonPending">结束本次请求</button></div></section>
    <div class="banks-layout">
      <aside class="panel banks-list"><div class="section-heading"><h3>{{archivedFilter?'归档题库':'使用中的题库'}}</h3><button class="text-button small" :disabled="locked" @click="loadBanks()">刷新题库</button></div><p v-if="!banksLoaded" class="hint">{{operationBusy?'正在读取题库…':'尚未读取完成，可以刷新重试。'}}</p><p v-else-if="!banks.length" class="hint">{{archivedFilter?'还没有归档题库。':mode==='manage'?'还没有题库，点击“新建题库”整理常用题目。':'还没有可用题库，请先到题库管理中建立并收录题目。'}}</p><button v-for="bank in banks" :key="bank.id" :class="['banks-entry',{active:selectedBank?.id===bank.id}]" :disabled="locked" @click="openBank(bank)"><strong>{{bank.name}}</strong><span>{{bank.description||'无题库说明'}}</span><small>{{bank.archived?'已归档':'使用中'}} · 修订{{bank.revision}}</small></button><div v-if="bankPrevious.length||bankNext" class="banks-pages"><button class="button secondary small" :disabled="locked||!bankPrevious.length" @click="loadPreviousBanks">上一页题库</button><span class="muted small">第{{bankPrevious.length+1}}页</span><button class="button secondary small" :disabled="locked||!bankNext" @click="loadNextBanks">下一页题库</button></div></aside>
      <div class="banks-main">
        <form v-if="formMode" class="panel banks-form" @submit.prevent="saveForm"><h3>{{formMode==='create'?'新建题库':'编辑题库信息'}}</h3><fieldset :disabled="locked"><label>题库名称<input v-model="formName" maxlength="120" placeholder="例如：学习习惯常用题"/></label><label>题库说明<textarea v-model="formDescription" maxlength="2000" rows="3" placeholder="说明这份题库适用于哪些问卷"/></label></fieldset><div class="button-row"><button type="submit" class="button primary" :disabled="locked||!formName.trim()">{{formMode==='create'?'创建题库':'保存题库信息'}}</button><button type="button" class="button secondary" :disabled="locked" @click="cancelForm">取消编辑</button></div></form>
        <section v-if="selectedBank" class="panel banks-detail"><div class="section-heading"><div><div class="eyebrow">{{selectedBank.archived?'已归档':'使用中'}} · 修订{{selectedBank.revision}}</div><h3>{{selectedBank.name}}</h3><p class="hint">{{selectedBank.description||'没有填写题库说明。'}}</p></div><div v-if="mode==='manage'" class="button-row"><button class="button secondary small" :disabled="locked||!!formMode" @click="startEdit">编辑题库信息</button><button class="button secondary small" :disabled="locked||!!formMode" @click="requestArchive">{{selectedBank.archived?'恢复题库':'归档题库'}}</button></div></div><p v-if="selectedBank.archived" class="banks-info">归档题库保留成员记录。恢复后可继续调整；已有问卷不会改变。</p><div class="section-heading banks-members-heading"><h4>题库成员</h4><div class="button-row"><button class="text-button small" :disabled="locked||!!formMode" @click="loadMembers">刷新成员</button><button v-if="mode==='manage'" class="button primary small" :disabled="!canManageMembers" @click="openAdd">收录已有题目</button></div></div><p v-if="!membersLoaded" class="hint">{{operationBusy?'正在读取成员…':'成员尚未读取完成，请刷新。'}}</p><p v-else-if="!visibleMembers.length" class="hint">{{mode==='pick'?'当前页没有符合条件的可用题目。若有后续页，可以继续查找。':'当前页没有题目。收录已有题目时会明确选择固定版本。'}}</p><article v-for="item in visibleMembers" :key="item.id" :class="['banks-member',{selected:selectedItemId===item.id}]"><div><button class="banks-member-title" :disabled="locked||!!formMode||!item.available" @click="selectItem(item)">{{item.available?item.version?.content.title:'当前不可用的题目'}}</button><p v-if="item.available&&item.version" class="hint">{{kindNames[item.version.content.kind]}} · 题库固定V{{item.version.versionNo}} <span v-if="item.currentVersionId&&item.currentVersionId!==item.questionVersionId">· 作者已有其他当前版本，本条目仍保持原版</span></p><p v-else class="hint">可能已撤销共享或来源当前不可读取。整理记录保留，不能预览、换版或从这里选用。</p></div><div v-if="mode==='manage'" class="button-row"><button class="button secondary small" :disabled="!canManageMembers||!item.available" @click="openUpdate(item)">更新所用版本</button><button class="text-button small" :disabled="!canManageMembers" @click="requestRemove(item)">移出题库</button></div><button v-else class="button secondary small" :disabled="locked||!item.available" @click="selectItem(item)">{{selectedItemId===item.id?'已选中':'查看固定版本'}}</button></article><div v-if="memberPrevious.length||memberNext" class="banks-pages"><button class="button secondary small" :disabled="locked||!!formMode||!memberPrevious.length" @click="loadPreviousMembers">上一页成员</button><span class="muted small">第{{memberPrevious.length+1}}页</span><button class="button secondary small" :disabled="locked||!!formMode||!memberNext" @click="loadNextMembers">下一页成员</button></div></section>
        <section v-if="previewQuestion&&selectedItem?.version" class="panel banks-preview"><div class="section-heading"><div><div class="eyebrow">固定V{{selectedItem.version.versionNo}} · 内容预览</div><h3>{{previewQuestion.title}}</h3></div><button v-if="mode==='pick'" class="button primary" :disabled="locked||!selectedItem.available||selectedBank?.archived" @click="chooseItem">选择题库V{{selectedItem.version.versionNo}}</button></div><AnswerInput v-model="previewAnswer" :question="previewQuestion" :disabled="operationBusy"/><p class="hint">预览回答仅保留在本页，不会提交答卷。题库中的固定版本只有显式更新后才会改变。</p></section>
        <section v-if="!selectedBank&&!formMode" class="panel empty-state"><h3>{{operationBusy?'正在读取…':'打开一份题库'}}</h3><p>从左侧选择题库，查看它保存的具体题目版本。</p></section>
      </div>
    </div>
    <div v-if="picker" class="modal-backdrop banks-picker-backdrop" @click.self="closePicker"><section class="modal banks-picker" role="dialog" aria-modal="true" aria-labelledby="bank-picker-title" @keydown.esc.stop="closePicker"><div class="section-heading"><div><h2 id="bank-picker-title">{{picker.title}}</h2><p class="hint">选择后保存到题库；问卷仍保留各自原有版本。共享使用权仍由题目作者控制。</p></div><button class="icon-button" aria-label="关闭题库选题窗口" :disabled="operationBusy" @click="closePicker">×</button></div><QuestionLibrary :key="picker.key" ref="pickerRef" mode="pick" destination="bank" :question-id="picker.questionId" :version-id="picker.versionId" @busy-change="picker.onBusy" @choose="picker.onChoose" @unauthorized="picker.onUnauthorized"/></section></div>
  </div>
</template>

<style scoped>
.banks-heading{align-items:flex-start;margin-bottom:20px}.banks-heading h2{margin:5px 0 8px;font-size:26px}.banks-heading .hint{max-width:760px;margin:0}.banks-filters{margin-bottom:20px}.banks-layout{display:grid;grid-template-columns:260px minmax(0,1fr);gap:22px;align-items:start}.banks-list{padding:20px;position:sticky;top:100px}.banks-list h3{font-size:16px;margin:0}.banks-entry{display:flex;flex-direction:column;gap:6px;text-align:left;width:100%;border:1px solid transparent;border-radius:10px;background:transparent;padding:14px 12px;margin-top:6px;color:inherit;cursor:pointer}.banks-entry:hover,.banks-entry.active{background:#edf5f1;border-color:#7da892}.banks-entry strong,.banks-member-title,.banks-detail h3{overflow-wrap:anywhere}.banks-entry span,.banks-entry small{color:var(--muted,#64748b);font-size:12px;overflow-wrap:anywhere}.banks-entry span{display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}.banks-main{min-width:0;display:grid;gap:20px}.banks-main>.panel{margin:0}.banks-pages{display:flex;align-items:center;justify-content:space-between;gap:6px;margin-top:18px}.banks-form fieldset{border:0;padding:0;margin:0 0 18px;min-width:0}.banks-form label+label{margin-top:18px}.banks-form textarea{resize:vertical}.banks-detail h3{margin:6px 0 8px;font-size:22px}.banks-detail>.section-heading{align-items:flex-start}.banks-members-heading{margin-top:25px;border-top:1px solid var(--line,#e5e9e7);padding-top:20px}.banks-members-heading h4{font-size:16px;margin:0}.banks-member{display:flex;align-items:flex-start;justify-content:space-between;gap:16px;padding:18px 12px;border-top:1px solid #e8edeb;margin:0 -12px}.banks-member.selected{background:#f0f7f3;border-radius:8px}.banks-member>div:first-child{min-width:0;flex:1}.banks-member-title{border:0;background:transparent;padding:0;font:inherit;font-weight:600;color:inherit;text-align:left;cursor:pointer;line-height:1.6}.banks-member>.button-row{flex-shrink:0;gap:10px}.banks-member .hint{margin:5px 0 0;line-height:1.6}.banks-preview h3{font-size:20px;line-height:1.5;overflow-wrap:anywhere}.banks-preview>.hint{margin-bottom:0}.banks-info,.banks-notice,.banks-error,.banks-confirm,.banks-conflict-state{border:1px solid #d8e4dc;border-radius:10px;padding:14px 18px;margin:14px 0;line-height:1.65}.banks-info{background:#f5f8f6}.banks-notice{background:#eff7f1;color:#245a38}.banks-error{background:#fff4f1;border-color:#edc6bd;color:#923f35}.banks-error p:last-child{margin-bottom:0}.banks-confirm{background:#f4faf6;border-color:#b7d4c3;margin-bottom:20px}.banks-confirm h3{margin-top:0}.banks-conflict-state{background:#f6f8f7}.banks-pending{margin-bottom:20px}.banks-pending h3{margin-top:0}.banks-pending .button-row{flex-wrap:wrap;gap:12px}.banks-picker-backdrop{z-index:75}.banks-picker{width:min(1120px,96vw);max-width:1120px;max-height:92vh;overflow:auto}.banks-picker>.section-heading{margin-bottom:24px;align-items:flex-start}.question-banks button:disabled{cursor:default}@media(max-width:1000px){.banks-layout{grid-template-columns:1fr}.banks-list{position:static}.banks-member{flex-wrap:wrap}.banks-member>.button-row{flex-shrink:1}.banks-heading h2{font-size:23px}}@media(max-width:600px){.banks-pages{flex-wrap:wrap}.banks-member>.button-row{width:100%}.banks-picker{padding:18px}.banks-heading{gap:14px}.banks-detail>.section-heading>.button-row{width:100%}}
</style>
