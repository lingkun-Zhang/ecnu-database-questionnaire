const assert = require('node:assert/strict')
const { readFileSync } = require('node:fs')
const { resolve } = require('node:path')
const { test: nodeTest } = require('node:test')
const { webcrypto } = require('node:crypto')
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
const bank = (id = 'bank-a', revision = 0, archived = false) => ({
  id, name: `题库 ${id}`, description: `说明 ${id}`, archived, revision,
  createdAt: '2026-10-26T00:00:00Z', updatedAt: '2026-10-26T00:00:00Z',
})
const version = (questionId = 'q-a', number = 1) => ({
  id: `${questionId}-v${number}`, questionId, versionNo: number,
  parentVersionId: number === 1 ? null : `${questionId}-v${number - 1}`,
  content: { kind: 'number', title: `内容 ${questionId} V${number}`, config: { min: 0, max: 7, integerOnly: true } },
  createdAt: '2026-10-26T00:00:00Z',
})
const item = (questionId = 'q-a', number = 1, available = true) => ({
  id: `item-${questionId}`, questionId, questionVersionId: `${questionId}-v${number}`,
  available, createdAt: '2026-10-26T00:00:00Z', updatedAt: '2026-10-26T00:00:00Z',
  ...(available ? { version: version(questionId, number), currentVersionId: `${questionId}-v2` } : {}),
})
const page = (items = [], nextAfterId = null) => ({ items, limit: 20, nextAfterId })
const url = call => new URL(`http://test${call.path}`)
const writes = h => h.calls.filter(call => call.method !== 'GET')
const reads = h => h.calls.filter(call => call.method === 'GET')
async function settle() {
  for (let turn = 0; turn < 3; turn++) { await vue.nextTick(); await new Promise(setImmediate) }
}

// Execute the actual setup and Vue reactivity. Only network, lifecycle and
// native confirmation delivery are controlled; these tests do not mount DOM.
function component(mode = 'manage', initialProps = {}) {
  const filename = resolve(__dirname, '../src/components/QuestionBanks.vue')
  const source = parse(readFileSync(filename, 'utf8')).descriptor.scriptSetup.content
  const exposed = 'banks,bankAfterId,bankPrevious,bankNext,archivedFilter,selectedBank,members,memberAfterId,memberPrevious,memberNext,selectedItemId,formMode,formName,formDescription,formBaseline,pendingWrite,confirmation,picker,pickerRef,conflictBank,conflictItem,conflictChecked,conflictNext,busy,failure,note,loadBanks,loadNextBanks,loadPreviousBanks,changeArchiveFilter,openBank,loadMembers,loadNextMembers,loadPreviousMembers,startCreate,startEdit,cancelForm,saveForm,requestArchive,requestRemove,confirmAction,cancelAction,openAdd,openUpdate,closePicker,acceptPickerVersion,selectItem,chooseItem,retryWrite,refreshSaved,reviewConflict,continueConflictReview,confirmConflict,submitReviewedWrite,abandonPending,onPickerBusy,canLeave,hasUnsavedChanges'
  const compile = value => ts.transpileModule(value, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText
  const props = vue.reactive({ mode, ...initialProps }), mounted = [], unmounted = [], calls = [], events = [], confirmations = []
  const modules = new Map(), effect = vue.effectScope()
  let token = 'account-A', confirmResult = true, disposed = false
  const network = { request: async call => { throw new Error(`Unexpected request: ${call.method} ${call.path}`) } }
  const api = { ApiError, getToken: () => token,
    request: async (path, options = {}) => {
      const call = { path, method: options.method || 'GET', body: options.body ? JSON.parse(options.body) : undefined,
        headers: Object.fromEntries(new Headers(options.headers)) }
      calls.push(call); return network.request(call)
    },
    post: async (path, body, headers = {}) => {
      const call = { path, method: 'POST', body: plain(body), headers: Object.fromEntries(new Headers(headers)) }
      calls.push(call); return network.request(call)
    },
  }
  const context = {
    URLSearchParams, Headers, crypto: webcrypto, setTimeout, clearTimeout,
    confirm: text => { confirmations.push(text); return confirmResult },
    document: { getElementById: () => null },
  }
  function resolveImport(name, from) {
    if (name === 'vue') return { ...vue, onMounted: fn => mounted.push(fn), onUnmounted: fn => unmounted.push(fn), onBeforeUnmount: fn => unmounted.push(fn) }
    if (/\/api$/.test(name)) return api
    if (name.endsWith('.vue')) return {}
    if (name.startsWith('.')) {
      const imported = resolve(from, '..', `${name}.ts`)
      if (modules.has(imported)) return modules.get(imported).exports
      const module = { exports: {} }; modules.set(imported, module)
      vm.runInNewContext(compile(readFileSync(imported, 'utf8')), { ...context, module, exports: module.exports, require: name => resolveImport(name, imported) })
      return module.exports
    }
    throw new Error(`Unexpected import: ${name}`)
  }
  const module = { exports: {} }
  effect.run(() => vm.runInNewContext(compile(source + `\nmodule.exports={${exposed}}`), {
    ...context, module, exports: module.exports, require: name => resolveImport(name, filename),
    defineProps: () => props, withDefaults: value => value,
    defineEmits: () => (name, ...args) => events.push({ name, args: plain(args) }), defineExpose: () => {},
  }))
  return { state: module.exports, props, network, calls, events, confirmations,
    setToken: value => { token = value }, setConfirm: value => { confirmResult = value },
    mount: async () => { await Promise.all(mounted.map(fn => fn())); await settle() },
    dispose: () => { if (!disposed) { disposed = true; unmounted.forEach(fn => fn()); effect.stop() } },
  }
}

function fixture(h, current = bank(), rows = [item()]) {
  const state = { current: plain(current), rows: plain(rows) }
  const read = async call => {
    const path = url(call).pathname
    if (call.method !== 'GET') throw new Error(`Unexpected write: ${call.method} ${call.path}`)
    if (path === '/question-banks') return page([plain(state.current)])
    if (path === `/question-banks/${state.current.id}`) return plain(state.current)
    if (path === `/question-banks/${state.current.id}/questions`) return { bank: plain(state.current), ...page(plain(state.rows)) }
    const match = path.match(/^\/questions\/([^/]+)\/versions\/([^/]+)$/)
    if (match) {
      const saved = state.rows.find(row => row.questionId === decodeURIComponent(match[1]) && row.questionVersionId === decodeURIComponent(match[2]))
      if (saved?.available && saved.version) return plain(saved.version)
      throw new ApiError(404, 'NOT_FOUND')
    }
    throw new Error(`Unexpected read: ${call.path}`)
  }
  h.network.request = read
  return { state, read }
}

async function open(h, server) {
  await h.mount(); await h.state.openBank(server.state.current)
}

test('bank cursors remain opaque and previous/archive navigation resets the cursor', async () => {
  const h = component(), a = bank(), b = bank('bank-b'), archived = bank('bank-old', 3, true)
  h.network.request = async call => {
    assert.equal(call.method, 'GET')
    if (url(call).searchParams.get('archived') === 'true') return page([archived])
    return url(call).searchParams.has('afterId') ? page([b]) : page([a], 'opaque%/bank?cursor')
  }
  try {
    await h.mount(); assert.equal(h.state.bankNext.value, 'opaque%/bank?cursor')
    await h.state.loadNextBanks()
    assert.equal(url(reads(h).at(-1)).searchParams.get('afterId'), 'opaque%/bank?cursor')
    assert.deepEqual(plain(h.state.banks.value), [b])
    await h.state.loadPreviousBanks()
    assert.equal(url(reads(h).at(-1)).searchParams.has('afterId'), false)
    assert.deepEqual(plain(h.state.bankPrevious.value), [])
    await h.state.changeArchiveFilter(true)
    assert.equal(url(reads(h).at(-1)).searchParams.get('archived'), 'true')
    assert.equal(url(reads(h).at(-1)).searchParams.has('afterId'), false)
    assert.deepEqual(plain(h.state.banks.value), [archived])
    assert.equal(h.state.selectedBank.value, null)
    assert.equal(writes(h).length, 0)
  } finally { h.dispose() }
})

test('member paging preserves unavailable records and never substitutes a question ID for the cursor', async () => {
  const h = component(), current = bank(), first = item(), unavailable = item('q-b', 1, false)
  const server = fixture(h, current)
  h.network.request = async call => url(call).pathname.endsWith('/questions')
    ? { bank: current, ...page(url(call).searchParams.has('afterId') ? [unavailable] : [first], url(call).searchParams.has('afterId') ? null : 'opaque%/member?next') }
    : server.read(call)
  try {
    await open(h, server); await h.state.loadNextMembers()
    assert.equal(url(reads(h).at(-1)).searchParams.get('afterId'), 'opaque%/member?next')
    assert.deepEqual(plain(h.state.members.value), [unavailable])
    await h.state.loadPreviousMembers()
    assert.equal(url(reads(h).at(-1)).searchParams.has('afterId'), false)
    assert.deepEqual(plain(h.state.members.value), [first])
    assert.equal(writes(h).length, 0)
  } finally { h.dispose() }
})

test('creating a bank captures one key and body then reads the saved bank without replaying the write', async () => {
  const h = component(), created = bank('new-bank'), server = fixture(h, created, [])
  h.network.request = async call => call.method === 'POST' ? plain(created) : server.read(call)
  try {
    await h.mount(); h.state.startCreate(); h.state.formName.value = '新题库'; h.state.formDescription.value = '用途说明'
    await h.state.saveForm()
    assert.equal(writes(h).length, 1)
    assert.equal(writes(h)[0].path, '/question-banks')
    assert.deepEqual(writes(h)[0].body, { name: '新题库', description: '用途说明' })
    assert.ok(writes(h)[0].headers['idempotency-key'])
    assert.equal(h.state.pendingWrite.value, null)
    assert.equal(h.state.selectedBank.value.id, created.id)
  } finally { h.dispose() }
})

test('network and server-unknown outcomes retry only the original bank creation key and body', async () => {
  for (const status of [0, 503]) {
    const h = component(), saved = bank('saved-bank'), server = fixture(h, saved, [])
    let attempts = 0
    h.network.request = async call => {
      if (call.method === 'POST') {
        if (++attempts === 1) throw new ApiError(status, status ? 'BANK_WRITE_OUTCOME_UNKNOWN' : 'NETWORK_ERROR')
        return plain(saved)
      }
      return server.read(call)
    }
    try {
      await h.mount(); h.state.startCreate(); h.state.formName.value = '待确认题库'; await h.state.saveForm()
      assert.equal(h.state.pendingWrite.value.phase, 'uncertain')
      const original = plain(writes(h)[0])
      h.state.formName.value = '不能替换已提交决定'
      await h.state.saveForm(); await h.state.loadBanks()
      assert.equal(attempts, 1)
      await h.state.retryWrite()
      assert.equal(attempts, 2)
      assert.deepEqual(plain(writes(h)[1]), original)
      assert.equal(h.state.pendingWrite.value, null)
    } finally { h.dispose() }
  }
})

test('a successful metadata PUT with failed read-back recovers with GET only', async () => {
  const h = component(), server = fixture(h)
  let failRead = false
  h.network.request = async call => {
    if (call.method === 'PUT') { server.state.current = { ...server.state.current, name: '已保存新名', revision: 1 }; failRead = true; return plain(server.state.current) }
    if (failRead) { failRead = false; throw new ApiError(503, 'DATABASE_UNAVAILABLE') }
    return server.read(call)
  }
  try {
    await open(h, server); h.state.startEdit(); h.state.formName.value = '已保存新名'; await h.state.saveForm()
    assert.equal(h.state.pendingWrite.value.phase, 'saved')
    assert.equal(writes(h).length, 1)
    await h.state.refreshSaved()
    assert.equal(writes(h).length, 1)
    assert.equal(h.state.pendingWrite.value, null)
    assert.equal(h.state.selectedBank.value.revision, 1)
  } finally { h.dispose() }
})

test('a conflicting decision needs a fresh read and explicit confirmation before a separate new write', async () => {
  const h = component(), server = fixture(h)
  let attempts = 0
  h.network.request = async call => {
    if (call.method === 'PUT') {
      if (++attempts === 1) { server.state.current = { ...server.state.current, name: '另一页面名称', revision: 4 }; throw new ApiError(409, 'BANK_CONFLICT') }
      server.state.current = { ...server.state.current, name: call.body.name, revision: 5 }
      return plain(server.state.current)
    }
    return server.read(call)
  }
  try {
    await open(h, server); h.state.startEdit(); h.state.formName.value = '我的新名称'; await h.state.saveForm()
    const original = plain(writes(h)[0].body)
    assert.equal(h.state.pendingWrite.value.phase, 'conflict')
    await h.state.retryWrite(); assert.equal(attempts, 1)
    await h.state.reviewConflict()
    assert.deepEqual(plain(h.state.pendingWrite.value.body), original)
    assert.equal(attempts, 1)
    h.state.confirmConflict()
    assert.equal(attempts, 1)
    assert.equal(h.state.pendingWrite.value.phase, 'ready')
    assert.equal(h.state.pendingWrite.value.body.expectedRevision, 4)
    await h.state.submitReviewedWrite()
    assert.equal(attempts, 2)
    assert.deepEqual(writes(h)[1].body, { ...original, expectedRevision: 4 })
    assert.equal(h.state.pendingWrite.value, null)
  } finally { h.dispose() }
})

test('metadata revision changes do not silently update the original pending body during conflict review', async () => {
  const h = component(), server = fixture(h)
  h.network.request = async call => {
    if (call.method === 'PUT') throw new ApiError(409, 'BANK_CONFLICT')
    return server.read(call)
  }
  try {
    await open(h, server); h.state.startEdit(); h.state.formName.value = '原决定'; await h.state.saveForm()
    const original = plain(h.state.pendingWrite.value.body)
    server.state.current.revision = 7; server.state.current.archived = true
    await h.state.reviewConflict()
    assert.deepEqual(plain(h.state.pendingWrite.value.body), original)
    assert.equal(writes(h).length, 1)
    assert.equal(h.state.pendingWrite.value.phase, 'conflict')
  } finally { h.dispose() }
})

test('member conflict review finds the target through opaque pages and rejects a revision change between pages', async () => {
  for (const changedDuringReview of [false, true]) {
    const h = component(), server = fixture(h)
    let attempts = 0
    h.network.request = async call => {
      if (call.method === 'PUT') {
        if (++attempts === 1) { server.state.current.revision = 8; throw new ApiError(409, 'BANK_CONFLICT') }
        server.state.current.revision = 9; server.state.rows = [item('q-a', 2)]; return plain(server.state.current)
      }
      if (attempts === 1 && url(call).pathname.endsWith('/questions')) {
        const later = url(call).searchParams.has('afterId')
        const reviewed = { ...server.state.current, revision: later && changedDuringReview ? 9 : 8 }
        return { bank: reviewed, ...page(later ? [{ ...item('q-a', 3), currentVersionId: 'q-a-v3' }] : [item('q-b')], later ? null : 'review%/next?member') }
      }
      return server.read(call)
    }
    try {
      await open(h, server); h.state.openUpdate(h.state.members.value[0]); await h.state.acceptPickerVersion(version('q-a', 2))
      const original = plain(h.state.pendingWrite.value.body)
      await h.state.reviewConflict()
      assert.equal(h.state.conflictChecked.value, false)
      await h.state.confirmConflict(); assert.equal(writes(h).length, 1)
      await h.state.continueConflictReview()
      assert.equal(url(reads(h).at(-1)).searchParams.get('afterId'), 'review%/next?member')
      assert.deepEqual(plain(h.state.pendingWrite.value.body), original)
      assert.equal(writes(h).length, 1)
      if (changedDuringReview) {
        assert.equal(h.state.failure.value.code, 'BANK_CONFLICT')
        assert.equal(h.state.conflictBank.value, null)
        assert.equal(h.state.conflictChecked.value, false)
        await h.state.confirmConflict(); assert.equal(writes(h).length, 1)
      } else {
        assert.equal(h.state.conflictChecked.value, true)
        assert.equal(h.state.conflictItem.value.questionVersionId, 'q-a-v3')
        await h.state.confirmConflict()
        assert.equal(h.state.pendingWrite.value.phase, 'ready')
        assert.deepEqual(plain(h.state.pendingWrite.value.body), { questionVersionId: 'q-a-v2', expectedRevision: 8 })
        assert.equal(writes(h).length, 1)
        await h.state.submitReviewedWrite()
        assert.equal(writes(h).length, 2)
      }
    } finally { h.dispose() }
  }
})

test('ending an uncertain creation requires in-page confirmation and keeps its local form for review', async () => {
  const h = component(), server = fixture(h)
  h.network.request = async call => call.method === 'POST' ? Promise.reject(new ApiError(503, 'BANK_WRITE_OUTCOME_UNKNOWN')) : server.read(call)
  try {
    await h.mount(); h.state.startCreate(); h.state.formName.value = '尚待核对'; await h.state.saveForm()
    const original = plain(h.state.pendingWrite.value)
    h.state.abandonPending()
    assert.equal(h.state.confirmation.value.kind, 'abandon')
    assert.deepEqual(plain(h.state.pendingWrite.value), original)
    h.state.cancelAction(); assert.deepEqual(plain(h.state.pendingWrite.value), original)
    h.state.abandonPending(); await h.state.confirmAction()
    assert.equal(h.state.pendingWrite.value, null)
    assert.equal(h.state.formName.value, '尚待核对')
    assert.equal(h.state.hasUnsavedChanges(), true)
    assert.equal(writes(h).length, 1)
    assert.equal(h.confirmations.length, 0)
  } finally { h.dispose() }
})

test('archive confirmation captures the original bank revision and cancellation performs no write', async () => {
  const h = component(), server = fixture(h)
  h.network.request = async call => {
    if (call.method === 'PUT') { server.state.current = { ...server.state.current, archived: true, revision: 1 }; return plain(server.state.current) }
    return server.read(call)
  }
  try {
    await open(h, server); h.state.requestArchive()
    assert.ok(h.state.confirmation.value)
    assert.equal(h.state.canLeave(), false)
    assert.equal(writes(h).length, 0)
    h.state.cancelAction(); assert.equal(h.state.confirmation.value, null)
    assert.equal(writes(h).length, 0)
    h.state.requestArchive()
    h.state.selectedBank.value.revision = 99; h.state.selectedBank.value.name = '迟到名称'
    await h.state.confirmAction()
    assert.deepEqual(writes(h)[0].body, { name: '题库 bank-a', description: '说明 bank-a', archived: true, expectedRevision: 0 })
    assert.equal(h.confirmations.length, 0)
  } finally { h.dispose() }
})

test('removal confirmation freezes the member identity and supports cancel without a request', async () => {
  const h = component(), server = fixture(h)
  h.network.request = async call => {
    if (call.method === 'DELETE') { server.state.rows = []; server.state.current.revision = 1; return plain(server.state.current) }
    return server.read(call)
  }
  try {
    await open(h, server); h.state.requestRemove(h.state.members.value[0]); h.state.cancelAction()
    assert.equal(writes(h).length, 0)
    h.state.requestRemove(h.state.members.value[0])
    h.state.members.value[0].questionId = 'wrong-question'
    await h.state.confirmAction()
    assert.equal(url(writes(h)[0]).pathname, '/question-banks/bank-a/questions/q-a')
    assert.equal(url(writes(h)[0]).searchParams.get('expectedRevision'), '0')
    assert.equal(h.state.pendingWrite.value, null)
  } finally { h.dispose() }
})

test('an unknown removal retries the same DELETE and recovers its success without interpreting the absent row as failure', async () => {
  const h = component(), server = fixture(h)
  let attempts = 0
  h.network.request = async call => {
    if (call.method === 'DELETE') {
      server.state.current.revision = 1; server.state.rows = []
      if (++attempts === 1) throw new ApiError(0, 'NETWORK_ERROR')
      return plain(server.state.current)
    }
    return server.read(call)
  }
  try {
    await open(h, server); h.state.requestRemove(h.state.members.value[0]); await h.state.confirmAction()
    assert.equal(h.state.pendingWrite.value.phase, 'uncertain')
    const original = plain(writes(h)[0])
    await h.state.retryWrite()
    assert.deepEqual(plain(writes(h)[1]), original)
    assert.equal(h.state.pendingWrite.value, null)
    assert.deepEqual(plain(h.state.members.value), [])
  } finally { h.dispose() }
})

test('adding a chosen version pins exactly that version and uses the current bank revision', async () => {
  const h = component(), server = fixture(h, bank('bank-a', 3), [])
  const chosen = version('q-b', 1)
  h.network.request = async call => {
    if (call.method === 'PUT') { server.state.current.revision = 4; server.state.rows = [item('q-b', 1)]; return plain(server.state.current) }
    return server.read(call)
  }
  try {
    await open(h, server); h.state.openAdd(); assert.ok(h.state.picker.value)
    await h.state.acceptPickerVersion(chosen)
    assert.equal(url(writes(h)[0]).pathname, '/question-banks/bank-a/questions/q-b')
    assert.deepEqual(writes(h)[0].body, { questionVersionId: 'q-b-v1', expectedRevision: 3 })
    assert.equal(h.state.picker.value, null)
    assert.equal(h.state.members.value[0].questionVersionId, 'q-b-v1')
    assert.equal(h.events.filter(event => event.name === 'choose').length, 0)
  } finally { h.dispose() }
})

test('updating a member locks its question series and rejects a different question result', async () => {
  const h = component(), server = fixture(h)
  try {
    await open(h, server); h.state.openUpdate(h.state.members.value[0])
    assert.ok(h.state.picker.value)
    await h.state.acceptPickerVersion(version('q-b', 2))
    assert.equal(writes(h).length, 0)
    assert.ok(h.state.picker.value)
  } finally { h.dispose() }
})

test('a child picker cannot submit its version until its busy state has ended', async () => {
  const h = component(), server = fixture(h, bank(), [])
  h.network.request = async call => {
    if (call.method === 'PUT') { server.state.current.revision = 1; server.state.rows = [item()]; return plain(server.state.current) }
    return server.read(call)
  }
  try {
    await open(h, server); h.state.openAdd(); h.state.onPickerBusy(true)
    await h.state.acceptPickerVersion(version())
    assert.equal(writes(h).length, 0)
    assert.ok(h.state.picker.value)
    h.state.onPickerBusy(false); await h.state.acceptPickerVersion(version())
    assert.equal(writes(h).length, 1)
    assert.equal(h.state.picker.value, null)
  } finally { h.dispose() }
})

test('unavailable members cannot be selected, previewed through choose, or used to update a pin', async () => {
  const h = component('pick'), server = fixture(h, bank(), [item('q-a', 1, false)])
  try {
    await open(h, server); h.state.selectItem(h.state.members.value[0]); await h.state.chooseItem()
    h.state.openUpdate(h.state.members.value[0])
    assert.equal(h.state.picker.value, null)
    assert.equal(h.events.filter(event => event.name === 'choose').length, 0)
    assert.equal(writes(h).length, 0)
  } finally { h.dispose() }
})

test('archived banks allow reading but block member changes and question selection', async () => {
  for (const mode of ['manage', 'pick']) {
    const h = component(mode), server = fixture(h, bank('bank-a', 2, true))
    try {
      await open(h, server); h.state.openAdd(); h.state.openUpdate(h.state.members.value[0]); h.state.requestRemove(h.state.members.value[0])
      h.state.selectItem(h.state.members.value[0]); await h.state.chooseItem()
      assert.equal(h.state.picker.value, null)
      assert.equal(h.state.confirmation.value, null)
      assert.equal(writes(h).length, 0)
      assert.equal(h.events.filter(event => event.name === 'choose').length, 0)
    } finally { h.dispose() }
  }
})

test('pick mode is read-only and a locked series cannot emit another question', async () => {
  const h = component('pick', { questionId: 'q-a' }), server = fixture(h, bank(), [item('q-a'), item('q-b')])
  try {
    await open(h, server)
    h.state.startCreate(); h.state.startEdit(); h.state.requestArchive(); h.state.openAdd(); h.state.requestRemove(h.state.members.value[0])
    h.state.selectItem(h.state.members.value[1]); await h.state.chooseItem()
    assert.equal(h.state.formMode.value, null)
    assert.equal(h.state.confirmation.value, null)
    assert.equal(h.state.picker.value, null)
    assert.equal(writes(h).length, 0)
    assert.equal(h.events.filter(event => event.name === 'choose').length, 0)
  } finally { h.dispose() }
})

test('choosing from a bank revalidates and emits the exact fixed version after clearing busy', async () => {
  const h = component('pick'), server = fixture(h)
  try {
    await open(h, server); h.state.selectItem(h.state.members.value[0]); await h.state.chooseItem()
    const chosen = h.events.filter(event => event.name === 'choose')
    assert.deepEqual(chosen, [{ name: 'choose', args: [version()] }])
    const choiceIndex = h.events.findIndex(event => event.name === 'choose')
    assert.deepEqual(h.events[choiceIndex - 1], { name: 'busy-change', args: [false] })
    assert.ok(reads(h).some(call => url(call).pathname === '/questions/q-a/versions/q-a-v1'))
    assert.equal(h.state.members.value[0].questionVersionId, 'q-a-v1')
    assert.equal(writes(h).length, 0)
  } finally { h.dispose() }
})

test('a revoked question during choose clears its cached content and cannot emit a stale version', async () => {
  const h = component('pick'), server = fixture(h)
  h.network.request = async call => url(call).pathname.startsWith('/questions/') ? Promise.reject(new ApiError(404, 'NOT_FOUND')) : server.read(call)
  try {
    await open(h, server); h.state.selectItem(h.state.members.value[0]); await h.state.chooseItem()
    assert.equal(h.events.filter(event => event.name === 'choose').length, 0)
    assert.equal(h.state.members.value[0].available, false)
    assert.equal(h.state.members.value[0].version, undefined)
    assert.equal(h.state.members.value[0].currentVersionId, undefined)
    await h.state.chooseItem()
    assert.equal(h.events.filter(event => event.name === 'choose').length, 0)
  } finally { h.dispose() }
})

test('bank revision changes detected while choosing require a new explicit selection', async () => {
  const h = component('pick'), server = fixture(h)
  try {
    await open(h, server); h.state.selectItem(h.state.members.value[0]); server.state.current.revision = 2
    await h.state.chooseItem()
    assert.equal(h.events.filter(event => event.name === 'choose').length, 0)
    assert.ok(h.state.failure.value)
    assert.equal(writes(h).length, 0)
  } finally { h.dispose() }
})

test('dirty forms, child busy states and pending confirmation protect leaving and navigation', async () => {
  const h = component(), server = fixture(h)
  try {
    await open(h, server); h.state.startEdit(); h.state.formName.value = '未保存名称'
    assert.equal(h.state.hasUnsavedChanges(), true)
    h.setConfirm(false); assert.equal(h.state.canLeave(), false)
    const before = reads(h).length
    await h.state.loadBanks(); assert.equal(reads(h).length, before)
    h.setConfirm(true); h.state.cancelForm()
    h.state.openAdd(); h.state.onPickerBusy(true)
    assert.equal(h.state.canLeave(), false)
    assert.deepEqual(h.events.at(-1), { name: 'busy-change', args: [true] })
    await h.state.openBank('bank-other'); assert.equal(reads(h).length, before)
    h.state.onPickerBusy(false)
    assert.deepEqual(h.events.at(-1), { name: 'busy-change', args: [false] })
    h.state.closePicker()
    h.state.requestArchive(); assert.equal(h.state.hasUnsavedChanges(), true)
    assert.equal(h.state.canLeave(), false)
    h.state.cancelAction(); assert.equal(h.state.hasUnsavedChanges(), false)
  } finally { h.dispose() }
})

test('an old write after token change or unmount cannot report success or issue another account read', async () => {
  for (const kind of ['token', 'unmount']) {
    const h = component(), server = fixture(h), late = deferred()
    h.network.request = async call => call.method === 'PUT' ? late.promise : server.read(call)
    try {
      await open(h, server); h.state.startEdit(); h.state.formName.value = '保存中'
      const work = h.state.saveForm(), readCount = reads(h).length
      if (kind === 'token') h.setToken('account-B')
      else h.dispose()
      const eventCount = h.events.length
      late.resolve(bank('bank-a', 1)); await work
      assert.equal(reads(h).length, readCount)
      assert.equal(h.events.length, eventCount)
      assert.equal(h.state.note.value.includes('已保存'), false)
    } finally { h.dispose() }
  }
})

test('current unauthorized reads notify the parent but late unauthorized results cannot clear a new session', async () => {
  for (const stale of [false, true]) {
    const h = component(), late = deferred()
    h.network.request = async () => late.promise
    try {
      const work = h.state.loadBanks()
      if (stale) h.setToken('account-B')
      late.reject(new ApiError(401, 'UNAUTHENTICATED')); await work
      assert.equal(h.events.filter(event => event.name === 'unauthorized').length, stale ? 0 : 1)
      if (stale) assert.equal(h.state.failure.value, null)
      else assert.equal(h.state.failure.value.status, 401)
    } finally { h.dispose() }
  }
})

test('a question prop change clears old selection and stale read completion cannot select another series', async () => {
  const h = component('pick', { questionId: 'q-a' }), server = fixture(h), late = deferred()
  let pausing = false
  h.network.request = async call => pausing && url(call).pathname === '/questions/q-a/versions/q-a-v1' ? late.promise : server.read(call)
  try {
    await open(h, server); h.state.selectItem(h.state.members.value[0]); pausing = true
    const work = h.state.chooseItem(); await settle()
    h.props.questionId = 'q-b'; await settle(); late.resolve(version('q-a')); await work
    assert.equal(h.events.filter(event => event.name === 'choose').length, 0)
    assert.equal(h.state.selectedItemId.value, '')
  } finally { h.dispose() }
})
