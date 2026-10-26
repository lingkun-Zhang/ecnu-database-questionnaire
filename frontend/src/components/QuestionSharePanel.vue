<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { ApiError, getToken, request } from '../api'

const props=defineProps<{questionId:string}>()
const emit=defineEmits<{'busy-change':[boolean];unauthorized:[]}>()
interface Share { id:string;questionId:string;recipientId:string;recipientUsername:string|null;active:boolean;revision:number;permission:'use';versionScope:'series';createdAt:string;updatedAt:string }
interface Page {items:Share[];nextAfterId:string|null}
interface Scope {token:string;generation:number;questionId:string}
interface ChangeBody {recipientUsername:string;active:boolean;expectedRevision:number}
interface ToggleConfirmation {row:Share;scope:Scope;active:boolean}
interface PendingChange {body:Readonly<ChangeBody>;recipientId?:string;phase:'uncertain'|'conflict'|'ready'|'saved';fromInput:boolean;inputValue:string;result?:Share}
const username=ref(''),rows=ref<Share[]>([]),nextAfterId=ref<string|null>(null),pageAfterId=ref<string|null>(null)
const previousPages=ref<Array<string|null>>([]),loaded=ref(false),busy=ref(false)
const failure=ref<ApiError|null>(null),note=ref(''),pendingChange=ref<PendingChange|null>(null),conflictRead=ref(false)
const pendingToggle=ref<ToggleConfirmation|null>(null),toggleCancelButton=ref<HTMLButtonElement|null>(null)
let generation=0,alive=true
class StaleShareResponse extends Error {}
const locked=computed(()=>busy.value||pendingChange.value!==null||pendingToggle.value!==null)
const canReadList=computed(()=>!busy.value&&!pendingToggle.value&&(!pendingChange.value||pendingChange.value.phase==='conflict'&&conflictRead.value))
function capture():Scope{return {token:getToken(),generation,questionId:props.questionId}}
function current(scope:Scope){return alive&&scope.token===getToken()&&scope.generation===generation&&scope.questionId===props.questionId}
function check(scope:Scope){if(!current(scope))throw new StaleShareResponse()}
function path(id=props.questionId){return `/questions/${encodeURIComponent(id)}/shares`}
function hasUnsavedChanges(){return Boolean(username.value.trim()||pendingChange.value||pendingToggle.value)}
function canLeave(){
  if(busy.value||pendingToggle.value)return false
  const operation=pendingChange.value
  if(operation?.phase==='uncertain')return confirm('共享操作的结果尚未确认，可能已经保存。离开会失去本页原请求重试入口，仍要离开？')
  if(operation?.phase==='saved')return confirm('共享操作已保存，但列表尚未读回。离开后可重新打开核对。确定离开？')
  if(operation)return confirm('共享操作还在等待你核对或提交，确定放弃本页这次操作？')
  return !username.value.trim()||confirm('输入的用户名尚未提交共享，确定放弃这项输入？')
}
function showFailure(error:unknown,scope:Scope){
  if(error instanceof StaleShareResponse||!current(scope))return
  failure.value=error instanceof ApiError?error:new ApiError(0,'CLIENT_ERROR',error instanceof Error?error.message:'操作未完成，请重试。')
  if(failure.value.status===401)emit('unauthorized')
}
async function run(action:(scope:Scope)=>Promise<void>){
  if(busy.value||!alive)return
  const scope=capture();busy.value=true;emit('busy-change',true);failure.value=null
  try{await action(scope);check(scope)}catch(error){showFailure(error,scope)}
  finally{if(current(scope)){busy.value=false;emit('busy-change',false)}}
}
async function readPage(afterId:string|null,scope:Scope){
  const params=new URLSearchParams({limit:'20'});if(afterId)params.set('afterId',afterId)
  const page=await request<Page>(`${path(scope.questionId)}?${params}`);check(scope);return page
}
function applyPage(page:Page,afterId:string|null){rows.value=page.items;nextAfterId.value=page.nextAfterId;pageAfterId.value=afterId;loaded.value=true}
async function loadShares(){
  if(!canReadList.value)return
  await run(async scope=>{const page=await readPage(pageAfterId.value,scope);check(scope);applyPage(page,pageAfterId.value)})
}
async function loadNext(){
  if(!canReadList.value||!nextAfterId.value)return
  const next=nextAfterId.value,previous=pageAfterId.value
  await run(async scope=>{const page=await readPage(next,scope);check(scope);previousPages.value=[...previousPages.value,previous];applyPage(page,next)})
}
async function loadPrevious(){
  if(!canReadList.value||!previousPages.value.length)return
  const previous=previousPages.value.at(-1)!
  await run(async scope=>{const page=await readPage(previous,scope);check(scope);previousPages.value=previousPages.value.slice(0,-1);applyPage(page,previous)})
}
function clearInputIfSubmitted(operation:PendingChange){if(operation.fromInput&&username.value===operation.inputValue)username.value=''}
async function readSaved(operation:PendingChange,scope:Scope){
  const page=await readPage(null,scope);check(scope);applyPage(page,null);previousPages.value=[]
  clearInputIfSubmitted(operation);pendingChange.value=null;conflictRead.value=false
  note.value=`${operation.body.active?'启用':'撤销'}共享的操作已保存，授权列表已刷新。`
}
function uncertain(error:unknown){return !(error instanceof ApiError)||error.status===0||error.status>=500}
async function executeChange(){
  if(busy.value||!pendingChange.value||pendingChange.value.phase==='conflict')return
  await run(async scope=>{
    const operation=pendingChange.value!
    if(operation.phase!=='saved'){
      try{
        const result=await request<Share>(path(scope.questionId),{method:'PUT',body:JSON.stringify(operation.body)});check(scope)
        operation.phase='saved';operation.result=result;pendingChange.value={...operation};clearInputIfSubmitted(operation)
        note.value='共享操作已保存，正在重新读取授权列表。'
      }catch(error){
        if(current(scope)){
          if(error instanceof ApiError&&error.status===409){operation.phase='conflict';pendingChange.value={...operation};conflictRead.value=false}
          else if(uncertain(error)){operation.phase='uncertain';pendingChange.value={...operation}}
          else pendingChange.value=null
        }
        throw error
      }
    }
    try{await readSaved(operation,scope)}catch(error){
      if(current(scope))note.value='共享操作已保存，但授权列表读取失败。请重新读取，不会再次更改共享。'
      throw error
    }
  })
}
async function grantUser(){
  if(locked.value)return
  const value=username.value.trim()
  if(!value){failure.value=new ApiError(422,'INVALID_USERNAME','请输入接收者已经注册的用户名。');return}
  pendingChange.value={body:Object.freeze({recipientUsername:value,active:true,expectedRevision:0}),phase:'uncertain',fromInput:true,inputValue:username.value}
  note.value='';await executeChange()
}
function toggleShare(row:Share){
  if(locked.value||!row.recipientUsername)return
  pendingToggle.value={row:{...row},scope:capture(),active:!row.active}
  failure.value=null;note.value=''
  void nextTick(()=>{const decision=pendingToggle.value;if(decision&&current(decision.scope))toggleCancelButton.value?.focus()})
}
async function confirmToggle(){
  const decision=pendingToggle.value
  if(!decision||busy.value||pendingChange.value||!current(decision.scope)||!decision.row.recipientUsername)return
  const row=decision.row
  pendingToggle.value=null
  pendingChange.value={body:Object.freeze({recipientUsername:row.recipientUsername!,active:decision.active,expectedRevision:row.revision}),recipientId:row.recipientId,phase:'uncertain',fromInput:false,inputValue:''}
  note.value='';await executeChange()
}
function cancelToggle(){
  const decision=pendingToggle.value
  if(!decision||busy.value||!current(decision.scope))return
  pendingToggle.value=null;note.value='已取消，本页没有提交共享修改。'
}
async function retryChange(){if(pendingChange.value?.phase==='uncertain')await executeChange()}
async function refreshSaved(){if(pendingChange.value?.phase==='saved')await executeChange()}
async function reviewConflict(){
  if(busy.value||pendingChange.value?.phase!=='conflict')return
  await run(async scope=>{const page=await readPage(null,scope);check(scope);applyPage(page,null);previousPages.value=[];conflictRead.value=true
    note.value='已重新读取列表。请找到对应用户的记录，再核对当前状态。不会自动再次提交；目标在后续页时可继续翻页。'})
}
function rebaseConflict(row:Share){
  const operation=pendingChange.value
  if(busy.value||operation?.phase!=='conflict'||!conflictRead.value||!row.recipientUsername)return
  if(operation.recipientId&&operation.recipientId!==row.recipientId){failure.value=new ApiError(409,'WRONG_SHARE_RECIPIENT','请核对原接收者的记录，或明确放弃本次请求后再操作其他用户。');return}
  const desired=operation.body.active?'启用':'撤销'
  if(!confirm(`原请求是向「${operation.body.recipientUsername}」${desired}共享。你选择的记录是「${row.recipientUsername}」，目前${row.active?'已启用':'已撤销'}，修订${row.revision}。确认这是要操作的用户，并按这份状态重新核对？确认后仍需点击提交。`))return
  failure.value=null;conflictRead.value=false
  if(row.active===operation.body.active){clearInputIfSubmitted(operation);pendingChange.value=null;note.value=`该用户目前已经${row.active?'启用':'撤销'}共享，已结束原请求，没有再次提交。`;return}
  pendingChange.value={...operation,body:Object.freeze({recipientUsername:row.recipientUsername,active:operation.body.active,expectedRevision:row.revision}),recipientId:row.recipientId,phase:'ready',result:undefined}
  note.value=`已核对「${row.recipientUsername}」的当前状态。尚未提交，请再次点击“提交已确认操作”。`
}
async function submitReviewedChange(){if(pendingChange.value?.phase==='ready')await executeChange()}
function abandonPending(){
  const operation=pendingChange.value;if(busy.value||!operation)return
  const message=operation.phase==='uncertain'?'上次操作可能已经保存，放弃后会失去本页原请求重试入口。确定放弃并自行核对授权列表？':operation.phase==='saved'?'操作已经保存，只是尚未读回列表。确定结束本次提示？':'原请求不会再次提交。确定放弃本次核对或待提交操作？'
  if(!confirm(message))return
  pendingChange.value=null;conflictRead.value=false;failure.value=null;note.value='已结束本次请求，可以重新读取列表后再明确操作。'
}
function reset(){generation++;username.value='';rows.value=[];nextAfterId.value=null;pageAfterId.value=null;previousPages.value=[];loaded.value=false;busy.value=false;failure.value=null;note.value='';pendingChange.value=null;pendingToggle.value=null;conflictRead.value=false;emit('busy-change',false)}
watch(()=>props.questionId,()=>{reset();void loadShares()})
onMounted(()=>loadShares())
onUnmounted(()=>{alive=false;generation++;pendingToggle.value=null})
defineExpose({canLeave,hasUnsavedChanges})
</script>

<template>
  <section class="panel question-share-panel" :aria-busy="busy" aria-labelledby="question-share-heading">
    <div class="section-heading"><div><h3 id="question-share-heading">共享给其他用户</h3><p class="hint">对方可读取这道题的历史和后续版本，在自己的问卷中选择固定版本；问卷不会自动换版。</p></div><button class="text-button small" :disabled="!canReadList" @click="loadShares">刷新授权</button></div>
    <p class="hint">共享只授予题目使用权，不授予编辑原题或查看答卷的权限。撤销后禁止新增引用和换版，已经保存的固定引用仍可保留。</p>
    <p v-if="note" class="share-notice" role="status">{{note}}</p>
    <div v-if="failure" class="share-error" role="alert"><strong>{{failure.message}}</strong><p v-for="(issue,index) in failure.fieldErrors" :key="index">{{issue.message}}</p></div>
    <form class="share-form" @submit.prevent="grantUser"><label>接收用户名<input v-model="username" :disabled="locked" maxlength="128" autocomplete="off" placeholder="输入对方已经注册的用户名"/></label><button type="submit" class="button primary" :disabled="locked||!username.trim()">授权使用</button></form>
    <div v-if="pendingToggle" class="share-confirmation" role="alertdialog" aria-labelledby="share-toggle-title" aria-describedby="share-toggle-description" @keydown.esc.prevent="cancelToggle">
      <h4 id="share-toggle-title">{{pendingToggle.active?'重新启用':'撤销'}}对「{{pendingToggle.row.recipientUsername}}」的共享？</h4>
      <p id="share-toggle-description">{{pendingToggle.active?'对方可以读取这道题的历史和后续版本，并在自己的问卷中选择固定版本。':'撤销后，对方不能新增引用或更换版本，已经保存的固定引用仍可保留。'}}题目管理权和答卷查看权限不会因此改变。</p>
      <p class="hint">这次操作尚未提交。若共享状态已在其他页面改变，提交时会要求重新核对。</p>
      <div class="button-row"><button class="button primary small" :disabled="busy" @click="confirmToggle">{{pendingToggle.active?'确认重新启用':'确认撤销共享'}}</button><button ref="toggleCancelButton" class="button secondary small" :disabled="busy" @click="cancelToggle">取消操作</button></div>
    </div>
    <div v-if="pendingChange" class="share-pending">
      <p v-if="pendingChange.phase==='uncertain'">上次结果尚未确认，请按原请求重试。确认前已暂停新的共享决定。</p>
      <p v-else-if="pendingChange.phase==='saved'">操作已保存。重新读取只刷新列表，不会重复更改共享。</p>
      <p v-else-if="pendingChange.phase==='ready'">已核对「{{pendingChange.body.recipientUsername}}」的状态，等待你提交{{pendingChange.body.active?'启用':'撤销'}}决定。</p>
      <p v-else>共享状态已经变化。请先重新读取，再从列表找到接收者的记录核对，不能直接重试旧的状态修改。</p>
      <div class="button-row"><button v-if="pendingChange.phase==='uncertain'" class="button secondary small" :disabled="busy" @click="retryChange">原样重试共享操作</button><button v-else-if="pendingChange.phase==='saved'" class="button secondary small" :disabled="busy" @click="refreshSaved">重新读取授权结果</button><button v-else-if="pendingChange.phase==='ready'" class="button primary small" :disabled="busy" @click="submitReviewedChange">提交已确认操作</button><button v-else class="button secondary small" :disabled="busy" @click="reviewConflict">重新读取并核对</button><button class="text-button small" :disabled="busy" @click="abandonPending">{{pendingChange.phase==='saved'?'结束结果提示':'放弃本次请求'}}</button></div>
    </div>
    <p v-if="!loaded" class="hint">{{busy?'正在读取授权…':'授权尚未读取，可刷新重试。'}}</p><p v-else-if="!rows.length" class="hint">当前页没有授权记录。</p>
    <ul v-else class="share-list"><li v-for="row in rows" :key="row.id"><div><strong>{{row.recipientUsername||'接收账号当前不可用'}}</strong><span class="share-state">{{row.active?'已启用':'已撤销'}}</span><small>状态修订 {{row.revision}}</small></div><button v-if="pendingChange?.phase==='conflict'&&conflictRead" class="button secondary small" :disabled="busy||!row.recipientUsername||!!pendingChange.recipientId&&pendingChange.recipientId!==row.recipientId" @click="rebaseConflict(row)">按此记录重新确认</button><button v-else class="button secondary small" :disabled="locked||!row.recipientUsername" :aria-label="`${row.active?'撤销':'重新启用'}对${row.recipientUsername||'该用户'}的共享`" @click="toggleShare(row)">{{row.active?'撤销共享':'重新启用'}}</button></li></ul>
    <div v-if="previousPages.length||nextAfterId" class="share-pages"><button class="button secondary small" :disabled="!canReadList||!previousPages.length" @click="loadPrevious">上一页授权</button><span class="muted small">第{{previousPages.length+1}}页</span><button class="button secondary small" :disabled="!canReadList||!nextAfterId" @click="loadNext">下一页授权</button></div>
  </section>
</template>

<style scoped>
.question-share-panel{margin-top:18px}.share-form{display:flex;align-items:flex-end;gap:12px;margin:18px 0}.share-form label{flex:1;margin:0}.share-form button{flex-shrink:0}.share-list{list-style:none;padding:0;margin:16px 0}.share-list li{display:flex;align-items:center;justify-content:space-between;gap:12px;border-top:1px solid var(--line,#e6eae8);padding:14px 0}.share-list small{display:block;margin-top:4px;color:var(--muted,#68756e)}.share-state{margin-left:10px;font-size:12px;color:var(--muted,#68756e)}.share-notice,.share-error,.share-pending{border:1px solid var(--line,#e0e7e3);border-radius:10px;padding:12px 14px;margin:12px 0}.share-notice{background:#eff7f2}.share-error{background:#fff4f2;color:#98483d}.share-error p:last-child,.share-pending p:first-child{margin-top:0}.share-confirmation{border:1px solid #c5d9cc;border-radius:10px;padding:16px;margin:14px 0;background:#f5faf6}.share-confirmation h4{margin:0 0 10px}.share-confirmation p{line-height:1.65}.share-pages{display:flex;align-items:center;justify-content:space-between;gap:10px}@media(max-width:600px){.share-form{align-items:stretch;flex-direction:column}.share-list li{align-items:flex-start;flex-direction:column}}
</style>
