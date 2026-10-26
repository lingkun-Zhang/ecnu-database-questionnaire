const assert = require('node:assert/strict')
const { readFileSync } = require('node:fs')
const { resolve } = require('node:path')
const { test: nodeTest } = require('node:test')
const vm = require('node:vm')
const ts = require('typescript')
const { parse } = require('@vue/compiler-sfc')
const vue = require('vue')

const test = (name, action) => nodeTest(name, { timeout: 5000 }, action)
const plain = value => JSON.parse(JSON.stringify(value))
const deferred = () => {
  let resolve, reject
  const promise = new Promise((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}
class ApiError extends Error {
  constructor(status, code, message = code, fieldErrors = []) {
    super(message); this.status = status; this.code = code; this.fieldErrors = fieldErrors
  }
}
const share = (id = 'share-a', username = 'receiver_a', active = true, revision = 1, questionId = 'q-a') => ({
  id, questionId, recipientId: `user-${id}`, recipientUsername: username, active, revision,
  permission: 'use', versionScope: 'series', createdAt: '2026-10-26T00:00:00Z', updatedAt: '2026-10-26T00:00:00Z',
})
const page = (items = [], nextAfterId = null) => ({ items, nextAfterId })
const readPath = call => new URL(`http://test${call.path}`)
const writes = h => h.calls.filter(call => call.method === 'PUT')
const reads = h => h.calls.filter(call => call.method === 'GET')
const falseBusyEvents = h => h.events.filter(event => event.name === 'busy-change' && event.args[0] === false).length
async function settle() {
  for (let turn = 0; turn < 3; turn++) { await vue.nextTick(); await new Promise(setImmediate) }
}

// Execute the actual script setup and actual Vue reactivity. HTTP responses and
// native confirmations are controlled; no sharing decision logic is duplicated.
function component(questionId = 'q-a') {
  const filename = resolve(__dirname, '../src/components/QuestionSharePanel.vue')
  const source = parse(readFileSync(filename, 'utf8')).descriptor.scriptSetup.content
  const exposed = 'username,rows,nextAfterId,pageAfterId,previousPages,loaded,busy,failure,note,pendingChange,pendingToggle,conflictRead,locked,canReadList,loadShares,loadNext,loadPrevious,grantUser,toggleShare,confirmToggle,cancelToggle,retryChange,refreshSaved,reviewConflict,rebaseConflict,submitReviewedChange,abandonPending,canLeave,hasUnsavedChanges'
  const code = ts.transpileModule(source + `\nmodule.exports={${exposed}}`, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText
  const props = vue.reactive({ questionId }), mounted = [], unmounted = [], calls = [], events = [], confirmations = []
  const effect = vue.effectScope()
  let token = 'account-A', confirmResult = true, disposed = false
  const network = { request: async call => { throw new Error(`Unexpected request: ${call.method} ${call.path}`) } }
  const api = { ApiError, getToken: () => token, request: async (path, options = {}) => {
    const call = { path, method: options.method || 'GET', body: options.body ? JSON.parse(options.body) : undefined }
    calls.push(call); return network.request(call)
  } }
  const module = { exports: {} }
  effect.run(() => vm.runInNewContext(code, {
    module, exports: module.exports, URLSearchParams,
    confirm: text => { confirmations.push(text); return confirmResult },
    require: name => {
      if (name === 'vue') return { ...vue, onMounted: fn => mounted.push(fn), onUnmounted: fn => unmounted.push(fn) }
      if (/\/api$/.test(name)) return api
      throw new Error(`Unexpected import: ${name}`)
    },
    defineProps: () => props, defineEmits: () => (name, ...args) => events.push({ name, args: plain(args) }),
    defineExpose: () => {},
  }))
  return { state: module.exports, props, network, calls, events, confirmations,
    setToken: value => { token = value }, setConfirm: value => { confirmResult = value },
    mount: () => Promise.all(mounted.map(fn => fn())),
    dispose: () => { if (!disposed) { disposed = true; unmounted.forEach(fn => fn()); effect.stop() } },
  }
}

test('share pages use opaque server cursors and restore the previous page without writes', async () => {
  const h = component(), first = share(), second = share('share-b', 'receiver_b')
  h.network.request = async call => readPath(call).searchParams.has('afterId') ? page([second]) : page([first], 'opaque-next%record')
  try {
    await h.mount()
    assert.equal(h.state.nextAfterId.value, 'opaque-next%record')
    await h.state.loadNext()
    assert.equal(readPath(reads(h)[1]).searchParams.get('afterId'), 'opaque-next%record')
    assert.equal(h.state.pageAfterId.value, 'opaque-next%record')
    assert.deepEqual(plain(h.state.rows.value), [second])
    await h.state.loadPrevious()
    assert.equal(readPath(reads(h)[2]).searchParams.has('afterId'), false)
    assert.equal(h.state.pageAfterId.value, null)
    assert.deepEqual(plain(h.state.previousPages.value), [])
    assert.deepEqual(plain(h.state.rows.value), [first])
    assert.equal(writes(h).length, 0)
  } finally { h.dispose() }
})

test('a new grant sends explicit revision zero and clears only its submitted username after reading back', async () => {
  const h = component(), gate = deferred(), saved = share('new', 'new_receiver')
  let putSeen = false
  h.network.request = async call => {
    if (call.method === 'PUT') { putSeen = true; return gate.promise }
    return page(putSeen ? [saved] : [])
  }
  try {
    await h.mount(); h.state.username.value = ' new_receiver '
    const saving = h.state.grantUser()
    assert.deepEqual(writes(h)[0].body, { recipientUsername: 'new_receiver', active: true, expectedRevision: 0 })
    assert.equal(h.state.busy.value, true)
    assert.equal(h.state.canLeave(), false)
    gate.resolve(saved); await saving
    assert.equal(h.state.username.value, '')
    assert.equal(h.state.pendingChange.value, null)
    assert.equal(h.state.hasUnsavedChanges(), false)
    assert.deepEqual(plain(h.state.rows.value), [saved])
  } finally { h.dispose() }
})

test('network and 503 outcomes retain the complete original decision and retry only that body', async () => {
  for (const status of [0, 503]) {
    const h = component(), original = share(), saved = share('target', 'target_receiver')
    let attempts = 0
    h.network.request = async call => {
      if (call.method === 'PUT') {
        if (++attempts === 1) throw new ApiError(status, status ? 'QUESTION_SHARE_WRITE_OUTCOME_UNKNOWN' : 'NETWORK_ERROR')
        return saved
      }
      return page([original, saved])
    }
    try {
      await h.mount(); h.state.username.value = 'target_receiver'; await h.state.grantUser()
      assert.equal(h.state.pendingChange.value.phase, 'uncertain')
      const body = plain(h.state.pendingChange.value.body)
      await h.state.toggleShare(original); await h.state.grantUser(); await h.state.loadShares()
      assert.equal(attempts, 1)
      assert.equal(h.state.locked.value, true)
      await h.state.retryChange()
      assert.deepEqual(writes(h).map(call => call.body), [body, body])
      assert.equal(h.state.pendingChange.value, null)
      assert.equal(h.state.username.value, '')
    } finally { h.dispose() }
  }
})

test('a confirmed PUT followed by failed read-back is recovered by GET without another PUT', async () => {
  const h = component(), saved = share('target', 'target_receiver')
  let readCount = 0
  h.network.request = async call => {
    if (call.method === 'PUT') return saved
    if (++readCount === 2) throw new ApiError(503, 'DATABASE_UNAVAILABLE')
    return page(readCount === 1 ? [] : [saved])
  }
  try {
    await h.mount(); h.state.username.value = 'target_receiver'; await h.state.grantUser()
    assert.equal(h.state.pendingChange.value.phase, 'saved')
    assert.equal(h.state.username.value, '')
    assert.equal(writes(h).length, 1)
    h.setConfirm(false); assert.equal(h.state.canLeave(), false)
    await h.state.refreshSaved()
    assert.equal(writes(h).length, 1)
    assert.equal(reads(h).length, 3)
    assert.equal(h.state.pendingChange.value, null)
    assert.deepEqual(plain(h.state.rows.value), [saved])
  } finally { h.dispose() }
})

test('409 review follows later pages and needs a separate confirmed submission with the read revision', async () => {
  const h = component(), unrelated = share(), target = share('target', 'receiver_name', false, 7)
  let attempts = 0
  h.network.request = async call => {
    if (call.method === 'PUT') {
      if (++attempts === 1) throw new ApiError(409, 'QUESTION_SHARE_CONFLICT')
      return { ...target, active: true, revision: 8 }
    }
    return readPath(call).searchParams.get('afterId') === 'review-next-page' ? page([target]) : page([unrelated], 'review-next-page')
  }
  try {
    await h.mount(); h.state.username.value = 'ＲＥＣＥＩＶＥＲ＿ＮＡＭＥ'; await h.state.grantUser()
    const old = plain(h.state.pendingChange.value.body)
    assert.equal(h.state.pendingChange.value.phase, 'conflict')
    await h.state.retryChange(); assert.equal(attempts, 1)
    await h.state.reviewConflict(); await h.state.loadNext()
    assert.equal(readPath(reads(h).at(-1)).searchParams.get('afterId'), 'review-next-page')
    assert.deepEqual(plain(h.state.pendingChange.value.body), old)
    h.state.rebaseConflict(h.state.rows.value[0])
    assert.equal(h.state.pendingChange.value.phase, 'ready')
    assert.deepEqual(plain(h.state.pendingChange.value.body), { recipientUsername: 'receiver_name', active: true, expectedRevision: 7 })
    assert.equal(attempts, 1)
    assert.match(h.confirmations.at(-1), /ＲＥＣＥＩＶＥＲ＿ＮＡＭＥ/)
    assert.match(h.confirmations.at(-1), /receiver_name/)
    await h.state.submitReviewedChange()
    assert.equal(attempts, 2)
    assert.equal(writes(h)[1].body.expectedRevision, 7)
    assert.equal(h.state.pendingChange.value, null)
  } finally { h.dispose() }
})

test('409 review ends an already achieved target without repeating a state change', async () => {
  const h = component(), initial = share(), current = { ...initial, active: false, revision: 2 }
  let attempts = 0
  h.network.request = async call => {
    if (call.method === 'PUT') { attempts++; throw new ApiError(409, 'QUESTION_SHARE_CONFLICT') }
    return page([attempts ? current : initial])
  }
  try {
    await h.mount(); h.state.toggleShare(h.state.rows.value[0])
    assert.equal(writes(h).length, 0)
    await h.state.confirmToggle()
    await h.state.reviewConflict(); h.state.rebaseConflict(h.state.rows.value[0])
    assert.equal(h.state.pendingChange.value, null)
    assert.match(h.state.note.value, /没有再次提交/)
    await h.state.submitReviewedChange(); await h.state.retryChange()
    assert.equal(attempts, 1)
  } finally { h.dispose() }
})

test('known-recipient conflicts reject another row and canceled confirmations preserve the original intent', async () => {
  const h = component(), original = share(), current = { ...original, active: false, revision: 2 }, other = share('other', 'another_receiver')
  let attempts = 0
  h.network.request = async call => {
    if (call.method === 'PUT') { attempts++; throw new ApiError(409, 'QUESTION_SHARE_CONFLICT') }
    return page(attempts ? [current, other] : [original, other])
  }
  try {
    await h.mount(); h.state.toggleShare(h.state.rows.value[0])
    assert.equal(writes(h).length, 0)
    await h.state.confirmToggle(); await h.state.reviewConflict()
    const before = plain(h.state.pendingChange.value.body)
    h.state.rebaseConflict(h.state.rows.value[1])
    assert.equal(h.state.failure.value.code, 'WRONG_SHARE_RECIPIENT')
    assert.deepEqual(plain(h.state.pendingChange.value.body), before)
    h.setConfirm(false); h.state.rebaseConflict(h.state.rows.value[0]); h.state.abandonPending()
    assert.equal(h.state.pendingChange.value.phase, 'conflict')
    assert.deepEqual(plain(h.state.pendingChange.value.body), before)
    assert.equal(attempts, 1)
  } finally { h.dispose() }
})

test('a question change discards its old write response without clearing the new question input or busy state', async () => {
  const h = component(), late = deferred(), oldShare = share('old', 'old_receiver'), newShare = share('new', 'new_receiver', true, 1, 'q-b')
  h.network.request = async call => call.method === 'PUT' ? late.promise : page(call.path.includes('/q-b/') ? [newShare] : [])
  try {
    await h.mount(); h.state.username.value = 'old_receiver'; const work = h.state.grantUser()
    h.props.questionId = 'q-b'; await settle()
    assert.deepEqual(plain(h.state.rows.value), [newShare])
    h.state.username.value = 'new_account_input'
    const before = falseBusyEvents(h), readCount = reads(h).length
    late.resolve(oldShare); await work
    assert.equal(h.state.username.value, 'new_account_input')
    assert.deepEqual(plain(h.state.rows.value), [newShare])
    assert.equal(falseBusyEvents(h), before)
    assert.equal(reads(h).length, readCount)
    assert.equal(h.state.failure.value, null)
  } finally { h.dispose() }
})

test('a late write after token change cannot acknowledge success or issue a read for the new account', async () => {
  const h = component(), late = deferred()
  h.network.request = async call => call.method === 'PUT' ? late.promise : page()
  try {
    await h.mount(); h.state.username.value = 'old_receiver'; const work = h.state.grantUser()
    const before = falseBusyEvents(h), readCount = reads(h).length
    h.setToken('account-B'); h.state.username.value = 'new_account_input'
    late.resolve(share('old', 'old_receiver')); await work
    assert.equal(h.state.username.value, 'new_account_input')
    assert.equal(h.state.pendingChange.value.phase, 'uncertain')
    assert.equal(h.state.note.value, '')
    assert.equal(h.state.failure.value, null)
    assert.equal(falseBusyEvents(h), before)
    assert.equal(reads(h).length, readCount)
  } finally { h.dispose() }
})

test('unmounting suppresses a late unauthorized error and the old completion busy event', async () => {
  const h = component(), late = deferred()
  h.network.request = async call => call.method === 'PUT' ? late.promise : page()
  await h.mount(); h.state.username.value = 'receiver_name'; const work = h.state.grantUser()
  const before = falseBusyEvents(h)
  h.dispose(); late.reject(new ApiError(401, 'UNAUTHENTICATED')); await work
  assert.equal(h.events.some(event => event.name === 'unauthorized'), false)
  assert.equal(falseBusyEvents(h), before)
  assert.equal(h.state.failure.value, null)
})

test('current 401 reads and writes emit unauthorized without retaining a repeatable write', async () => {
  for (const stage of ['read', 'write']) {
    const h = component()
    h.network.request = async call => {
      if (stage === 'read' || call.method === 'PUT') throw new ApiError(401, 'UNAUTHENTICATED')
      return page()
    }
    try {
      await h.mount()
      if (stage === 'write') { h.state.username.value = 'receiver_name'; await h.state.grantUser() }
      assert.equal(h.events.filter(event => event.name === 'unauthorized').length, 1)
      assert.equal(h.state.failure.value.status, 401)
      assert.equal(h.state.pendingChange.value, null)
      assert.equal(h.state.busy.value, false)
    } finally { h.dispose() }
  }
})

test('dirty inputs and unknown outcomes require explicit leave or abandon while busy requests cannot leave', async () => {
  const h = component(), late = deferred()
  h.network.request = async call => call.method === 'PUT' ? late.promise : page()
  try {
    await h.mount(); h.state.username.value = 'receiver_name'
    assert.equal(h.state.hasUnsavedChanges(), true)
    h.setConfirm(false); assert.equal(h.state.canLeave(), false)
    assert.equal(h.state.username.value, 'receiver_name')
    const work = h.state.grantUser(), confirmationsBeforeBusyLeave = h.confirmations.length
    assert.equal(h.state.canLeave(), false)
    assert.equal(h.confirmations.length, confirmationsBeforeBusyLeave)
    late.reject(new ApiError(0, 'NETWORK_ERROR')); await work
    assert.equal(h.state.hasUnsavedChanges(), true)
    assert.equal(h.state.canLeave(), false)
    assert.match(h.confirmations.at(-1), /结果尚未确认/)
    h.state.abandonPending(); assert.equal(h.state.pendingChange.value.phase, 'uncertain')
    h.setConfirm(true); h.state.abandonPending()
    assert.equal(h.state.pendingChange.value, null)
    assert.equal(h.state.username.value, 'receiver_name')
    assert.equal(writes(h).length, 1)
  } finally { h.dispose() }
})


test('the in-page toggle confirmation locks other decisions and cancellation submits nothing', async () => {
  const h = component(), original = share()
  h.network.request = async () => page([original])
  try {
    await h.mount(); h.state.toggleShare(h.state.rows.value[0])
    assert.equal(writes(h).length, 0)
    assert.equal(h.confirmations.length, 0)
    assert.equal(h.state.pendingToggle.value.active, false)
    assert.equal(h.state.locked.value, true)
    assert.equal(h.state.canReadList.value, false)
    assert.equal(h.state.hasUnsavedChanges(), true)
    assert.equal(h.state.canLeave(), false)
    const before = reads(h).length
    await h.state.loadShares(); await h.state.loadNext(); await h.state.grantUser()
    assert.equal(reads(h).length, before)
    assert.equal(writes(h).length, 0)
    h.state.cancelToggle()
    assert.equal(h.state.pendingToggle.value, null)
    assert.equal(h.state.hasUnsavedChanges(), false)
    assert.equal(h.state.canLeave(), true)
    assert.equal(writes(h).length, 0)
  } finally { h.dispose() }
})

test('confirming a toggle submits the captured row and revision even if the visible row later changes', async () => {
  const h = component(), original = share('original', 'original_receiver', true, 4)
  const saved = { ...original, active: false, revision: 5 }
  h.network.request = async call => call.method === 'PUT' ? saved : page([original])
  try {
    await h.mount(); h.state.toggleShare(h.state.rows.value[0])
    assert.equal(writes(h).length, 0)
    h.state.rows.value[0].recipientUsername = 'different_receiver'
    h.state.rows.value[0].recipientId = 'different-id'
    h.state.rows.value[0].revision = 99
    h.state.rows.value[0].active = false
    assert.equal(h.state.pendingToggle.value.row.recipientUsername, 'original_receiver')
    assert.equal(h.state.pendingToggle.value.row.revision, 4)
    await h.state.confirmToggle()
    assert.deepEqual(writes(h).map(call => call.body), [{ recipientUsername: 'original_receiver', active: false, expectedRevision: 4 }])
    assert.equal(h.state.pendingToggle.value, null)
    assert.equal(h.state.pendingChange.value, null)
    assert.equal(h.confirmations.length, 0)
  } finally { h.dispose() }
})

test('expired token, changed question and unmount cannot confirm a captured toggle', async () => {
  for (const kind of ['token', 'question', 'unmount']) {
    const h = component(), original = share()
    h.network.request = async () => page([original])
    try {
      await h.mount(); h.state.toggleShare(h.state.rows.value[0])
      if (kind === 'token') h.setToken('account-B')
      else if (kind === 'question') { h.props.questionId = 'q-b'; await settle() }
      else h.dispose()
      const readCount = reads(h).length, events = h.events.length
      await h.state.confirmToggle()
      assert.equal(writes(h).length, 0)
      assert.equal(reads(h).length, readCount)
      assert.equal(h.events.length, events)
      if (kind !== 'token') assert.equal(h.state.pendingToggle.value, null)
    } finally { h.dispose() }
  }
})
