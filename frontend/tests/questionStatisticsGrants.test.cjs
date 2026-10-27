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
  constructor(status, code, message = code) { super(message); this.status = status; this.code = code }
}
const candidate = (id = 'question-a', active = false, revision = 0) => ({
  questionId: id, questionTitle: `发布过的题目 ${id}`, recipientUsername: `作者_${id}`,
  grant: revision ? { id: `grant-${id}`, active, revision, recipientUsername: `作者_${id}` } : null,
})
const writes = h => h.calls.filter(call => call.method !== 'GET')
const reads = h => h.calls.filter(call => call.method === 'GET')
const unauthorized = h => h.events.filter(event => event.name === 'unauthorized')
async function settle() {
  for (let turn = 0; turn < 3; turn++) { await vue.nextTick(); await new Promise(setImmediate) }
}

// Run the real setup with Vue's reactivity. Network, lifecycle delivery and
// native confirmations are controlled; these are not DOM or browser tests.
function component(surveyId = 'survey-a') {
  const filename = resolve(__dirname, '../src/components/QuestionStatisticsGrantPanel.vue')
  const source = parse(readFileSync(filename, 'utf8')).descriptor.scriptSetup.content
  const exposed = 'rows,busy,loaded,failure,note,pending,load,decide,cancel,submit,refreshSaved,abandon,canLeave,hasUnsavedChanges'
  const compiled = ts.transpileModule(source + `\nmodule.exports={${exposed}}`, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText
  const props = vue.reactive({ surveyId }), calls = [], events = [], confirmations = [], mounted = [], unmounted = []
  const effect = vue.effectScope(), module = { exports: {} }
  let token = 'account-A', confirmResult = true, disposed = false
  const network = { request: async call => { throw new Error(`Unexpected request: ${call.method} ${call.path}`) } }
  const api = { ApiError, getToken: () => token,
    request: async (path, options = {}) => {
      const call = { path, method: options.method || 'GET', body: options.body ? JSON.parse(options.body) : undefined }
      calls.push(call); return network.request(call)
    },
  }
  effect.run(() => vm.runInNewContext(compiled, {
    module, exports: module.exports,
    require: name => name === 'vue'
      ? { ...vue, onMounted: fn => mounted.push(fn), onUnmounted: fn => unmounted.push(fn) }
      : name === '../api' ? api : (() => { throw new Error(`Unexpected import: ${name}`) })(),
    defineProps: () => props, defineEmits: () => (name, ...args) => events.push({ name, args: plain(args) }), defineExpose: () => {},
    confirm: text => { confirmations.push(text); return confirmResult },
  }))
  return { state: module.exports, props, network, calls, events, confirmations,
    setToken: value => { token = value }, setConfirm: value => { confirmResult = value },
    mount: async () => { await Promise.all(mounted.map(fn => fn())); await settle() },
    dispose: () => { if (!disposed) { disposed = true; unmounted.forEach(fn => fn()); effect.stop() } },
  }
}
function fixture(h, rows = [candidate()]) {
  const server = { rows: plain(rows) }
  const read = async call => {
    assert.equal(call.method, 'GET')
    assert.match(call.path, /^\/surveys\/[^/]+\/question-statistics-grants$/)
    return { items: plain(server.rows) }
  }
  h.network.request = read
  return { server, read }
}

test('grant candidates come from the published-source endpoint and loading changes no grants', async () => {
  const h = component('survey /?中'), row = candidate('opaque /?题', true, 7)
  fixture(h, [row])
  try {
    await h.mount()
    assert.equal(reads(h)[0].path, '/surveys/survey%20%2F%3F%E4%B8%AD/question-statistics-grants')
    assert.deepEqual(plain(h.state.rows.value), [row])
    assert.equal(h.state.loaded.value, true)
    assert.equal(h.state.busy.value, false)
    assert.equal(h.state.pending.value, null)
    assert.equal(writes(h).length, 0)
    assert.deepEqual(h.events.filter(e => e.name === 'busy-change').map(e => e.args[0]), [true, false])
    assert.equal(h.state.canLeave(), true)
    assert.equal(h.state.hasUnsavedChanges(), false)
  } finally { h.dispose() }
})

test('an empty published-source list is a loaded empty result rather than an inferred authorization', async () => {
  const h = component(); fixture(h, [])
  try { await h.mount(); assert.equal(h.state.loaded.value, true); assert.deepEqual(plain(h.state.rows.value), []); assert.equal(writes(h).length, 0) }
  finally { h.dispose() }
})

test('choosing a candidate only opens inline confirmation and cancelling sends no write', async () => {
  const h = component(); fixture(h)
  try {
    await h.mount(); h.state.decide(h.state.rows.value[0], true)
    assert.equal(h.state.pending.value.phase, 'confirm')
    assert.deepEqual(plain(h.state.pending.value.body), { active: true, expectedRevision: 0 })
    assert.equal(h.state.pending.value.recipient, '作者_question-a')
    assert.equal(h.confirmations.length, 0)
    assert.equal(writes(h).length, 0)
    assert.equal(h.state.hasUnsavedChanges(), true)
    h.state.cancel(); assert.equal(h.state.pending.value, null)
    assert.equal(h.state.hasUnsavedChanges(), false)
    assert.equal(writes(h).length, 0)
  } finally { h.dispose() }
})

test('confirmed grant and revoke use the captured question and revision then read authoritative state', async () => {
  for (const active of [true, false]) {
    const h = component('survey /'), row = candidate('question /?中', !active, active ? 0 : 7), server = fixture(h, [row])
    h.network.request = async call => {
      if (call.method === 'PUT') { server.server.rows = [candidate(row.questionId, active, active ? 1 : 8)]; return plain(server.server.rows[0].grant) }
      return server.read(call)
    }
    try {
      await h.mount(); h.state.decide(h.state.rows.value[0], active); await h.state.submit()
      assert.equal(writes(h).length, 1)
      assert.equal(writes(h)[0].path, '/surveys/survey%20%2F/question-statistics-grants/question%20%2F%3F%E4%B8%AD')
      assert.deepEqual(writes(h)[0].body, { active, expectedRevision: active ? 0 : 7 })
      assert.equal(reads(h).length, 2)
      assert.equal(h.state.rows.value[0].grant.active, active)
      assert.equal(h.state.pending.value, null)
      assert.ok(h.state.note.value.includes(active ? '已授权' : '已撤销'))
      assert.equal(h.state.busy.value, false)
    } finally { h.dispose() }
  }
})

test('unknown PUT outcomes freeze the original decision and retry that body without rebasing', async () => {
  for (const failure of [new ApiError(0, 'NETWORK_ERROR'), new ApiError(503, 'STATISTICS_GRANT_WRITE_OUTCOME_UNKNOWN'), new Error('connection interrupted')]) {
    const h = component(), server = fixture(h, [candidate('question-a', true, 7)]); let attempts = 0
    h.network.request = async call => {
      if (call.method === 'PUT') {
        if (++attempts === 1) throw failure
        server.server.rows = [candidate('question-a', false, 8)]; return plain(server.server.rows[0].grant)
      }
      return server.read(call)
    }
    try {
      await h.mount(); h.state.decide(h.state.rows.value[0], false); await h.state.submit()
      assert.equal(h.state.pending.value.phase, 'uncertain')
      assert.equal(h.state.busy.value, false)
      const original = plain(writes(h)[0])
      h.state.rows.value[0].grant.revision = 200
      h.state.decide(candidate('unrelated', false, 90), true); h.state.cancel(); await h.state.load(); await h.state.refreshSaved()
      assert.equal(reads(h).length, 1)
      assert.equal(h.state.pending.value.questionId, 'question-a')
      await h.state.submit()
      assert.equal(attempts, 2)
      assert.deepEqual(plain(writes(h)[1]), original)
      assert.equal(h.state.pending.value, null)
      assert.equal(h.state.rows.value[0].grant.revision, 8)
    } finally { h.dispose() }
  }
})

test('a saved PUT with failed read-back recovers by GET without repeating the write', async () => {
  const h = component(), server = fixture(h); let failRead = false
  h.network.request = async call => {
    if (call.method === 'PUT') { server.server.rows = [candidate('question-a', true, 1)]; failRead = true; return plain(server.server.rows[0].grant) }
    if (failRead) { failRead = false; throw new ApiError(503, 'DATABASE_UNAVAILABLE') }
    return server.read(call)
  }
  try {
    await h.mount(); h.state.decide(h.state.rows.value[0], true); await h.state.submit()
    assert.equal(h.state.pending.value.phase, 'saved')
    await h.state.submit(); await h.state.load()
    assert.equal(writes(h).length, 1)
    assert.equal(reads(h).length, 2)
    await h.state.refreshSaved()
    assert.equal(reads(h).length, 3)
    assert.equal(writes(h).length, 1)
    assert.equal(h.state.rows.value[0].grant.revision, 1)
    assert.equal(h.state.pending.value, null)
    assert.equal(h.state.failure.value, '')
  } finally { h.dispose() }
})

test('a conflict blocks retries and rebasing until explicit abandonment reads the current grant', async () => {
  const h = component(), server = fixture(h, [candidate('question-a', true, 3)]); let attempts = 0
  h.network.request = async call => {
    if (call.method === 'PUT') {
      if (++attempts === 1) { server.server.rows = [candidate('question-a', false, 5)]; throw new ApiError(409, 'STATISTICS_GRANT_CONFLICT') }
      server.server.rows = [candidate('question-a', true, 6)]; return plain(server.server.rows[0].grant)
    }
    return server.read(call)
  }
  try {
    await h.mount(); h.state.decide(h.state.rows.value[0], false); await h.state.submit()
    assert.equal(h.state.pending.value.phase, 'conflict')
    await h.state.submit(); await h.state.load(); await h.state.refreshSaved(); h.state.cancel()
    assert.equal(writes(h).length, 1); assert.equal(reads(h).length, 1)
    h.setConfirm(false); await h.state.abandon()
    assert.equal(h.state.pending.value.phase, 'conflict'); assert.equal(reads(h).length, 1)
    h.setConfirm(true); await h.state.abandon()
    assert.equal(h.state.pending.value, null); assert.equal(reads(h).length, 2)
    assert.equal(h.state.rows.value[0].grant.revision, 5); assert.equal(writes(h).length, 1)
    h.state.decide(h.state.rows.value[0], true); assert.equal(writes(h).length, 1)
    await h.state.submit()
    assert.deepEqual(writes(h)[1].body, { active: true, expectedRevision: 5 })
    assert.equal(h.state.rows.value[0].grant.revision, 6)
  } finally { h.dispose() }
})

test('abandoning an unknown operation explicitly reads current state without issuing its PUT again', async () => {
  const h = component(), server = fixture(h)
  h.network.request = async call => call.method === 'PUT' ? Promise.reject(new ApiError(503, 'STATISTICS_GRANT_WRITE_OUTCOME_UNKNOWN')) : server.read(call)
  try {
    await h.mount(); h.state.decide(h.state.rows.value[0], true); await h.state.submit()
    server.server.rows = [candidate('question-a', true, 1)]
    h.setConfirm(true); await h.state.abandon()
    assert.equal(writes(h).length, 1); assert.equal(reads(h).length, 2)
    assert.equal(h.state.pending.value, null); assert.equal(h.state.rows.value[0].grant.active, true)
  } finally { h.dispose() }
})

test('current-session 401 during a PUT or its read-back clears private rows and informs the parent', async () => {
  for (const stage of ['put', 'read-back']) {
    const h = component(), server = fixture(h); let saved = false
    h.network.request = async call => {
      if (call.method === 'PUT') { if (stage === 'put') throw new ApiError(401, 'UNAUTHENTICATED'); saved = true; return {} }
      if (saved) throw new ApiError(401, 'UNAUTHENTICATED')
      return server.read(call)
    }
    try {
      await h.mount(); h.state.decide(h.state.rows.value[0], true); await h.state.submit()
      assert.equal(unauthorized(h).length, 1)
      assert.deepEqual(plain(h.state.rows.value), [])
      assert.equal(h.state.pending.value, null)
      assert.equal(h.state.busy.value, false)
    } finally { h.dispose() }
  }
})

test('definitive non-409 client rejection ends the decision and does not retry automatically', async () => {
  const h = component(), server = fixture(h)
  h.network.request = async call => call.method === 'PUT' ? Promise.reject(new ApiError(422, 'INVALID_STATISTICS_GRANT')) : server.read(call)
  try {
    await h.mount(); h.state.decide(h.state.rows.value[0], true); await h.state.submit(); await h.state.submit()
    assert.equal(h.state.pending.value, null); assert.equal(writes(h).length, 1)
    assert.equal(h.state.failure.value, 'INVALID_STATISTICS_GRANT'); assert.equal(unauthorized(h).length, 0)
  } finally { h.dispose() }
})

test('late old-token GET success or 401 cannot replace rows or expire the new session', async () => {
  for (const rejected of [false, true]) {
    const h = component(), gate = deferred(); h.network.request = () => gate.promise
    try {
      const running = h.state.load(); h.setToken('account-B')
      if (rejected) gate.reject(new ApiError(401, 'UNAUTHENTICATED'))
      else gate.resolve({ items: [candidate('old-account-data')] })
      await running
      assert.deepEqual(plain(h.state.rows.value), [])
      assert.equal(h.state.loaded.value, false)
      assert.equal(h.state.failure.value, '')
      assert.equal(unauthorized(h).length, 0)
    } finally { h.dispose() }
  }
})

test('late old-token PUT success or 401 cannot start read-back or emit unauthorized', async () => {
  for (const rejected of [false, true]) {
    const h = component(), server = fixture(h), gate = deferred()
    try {
      await h.mount(); h.network.request = call => call.method === 'PUT' ? gate.promise : server.read(call)
      h.state.decide(h.state.rows.value[0], true); const running = h.state.submit(); h.setToken('account-B')
      if (rejected) gate.reject(new ApiError(401, 'UNAUTHENTICATED'))
      else gate.resolve({ revision: 1 })
      await running
      assert.equal(reads(h).length, 1)
      assert.equal(h.state.pending.value.phase, 'uncertain')
      assert.equal(h.state.rows.value[0].grant, null)
      assert.equal(h.state.note.value, '')
      assert.equal(unauthorized(h).length, 0)
    } finally { h.dispose() }
  }
})

test('changing survey starts a new read and late old-survey GET cannot overwrite it or clear its busy state', async () => {
  for (const rejected of [false, true]) {
    const h = component(), oldGate = deferred(), newGate = deferred()
    h.network.request = call => call.path.includes('/survey-a/') ? oldGate.promise : newGate.promise
    try {
      const oldRead = h.state.load(); h.props.surveyId = 'survey-b'; await settle()
      assert.equal(reads(h).length, 2); assert.equal(h.state.busy.value, true)
      if (rejected) oldGate.reject(new ApiError(401, 'UNAUTHENTICATED'))
      else oldGate.resolve({ items: [candidate('old-survey')] })
      await oldRead
      assert.equal(h.state.busy.value, true); assert.deepEqual(plain(h.state.rows.value), [])
      newGate.resolve({ items: [candidate('new-survey')] }); await settle()
      assert.equal(h.state.rows.value[0].questionId, 'new-survey')
      assert.equal(h.state.busy.value, false); assert.equal(unauthorized(h).length, 0)
    } finally { h.dispose() }
  }
})

test('changing survey discards old pending state and ignores a late old-survey PUT', async () => {
  const h = component(), server = fixture(h), gate = deferred()
  try {
    await h.mount()
    h.network.request = call => call.method === 'PUT' ? gate.promise : Promise.resolve({ items: [candidate('new-survey')] })
    h.state.decide(h.state.rows.value[0], true); const writing = h.state.submit()
    h.props.surveyId = 'survey-b'; await settle()
    assert.equal(h.state.pending.value, null); assert.equal(h.state.rows.value[0].questionId, 'new-survey')
    gate.resolve({ revision: 1 }); await writing
    assert.equal(reads(h).length, 2); assert.equal(h.state.note.value, '')
    assert.equal(h.state.rows.value[0].questionId, 'new-survey'); assert.equal(unauthorized(h).length, 0)
  } finally { h.dispose() }
})

test('unmount releases parent busy state and late GET or PUT responses make no further changes', async () => {
  for (const stage of ['get', 'put']) for (const rejected of [false, true]) {
    const h = component(), server = fixture(h), gate = deferred()
    try {
      let running
      if (stage === 'get') { h.network.request = () => gate.promise; running = h.state.load() }
      else { await h.mount(); h.network.request = () => gate.promise; h.state.decide(h.state.rows.value[0], true); running = h.state.submit() }
      h.dispose(); const before = { rows: plain(h.state.rows.value), pending: plain(h.state.pending.value), events: plain(h.events), failure: h.state.failure.value }
      assert.deepEqual(h.events.at(-1), { name: 'busy-change', args: [false] })
      if (rejected) gate.reject(new ApiError(401, 'UNAUTHENTICATED'))
      else gate.resolve(stage === 'get' ? { items: [candidate('late')] } : { revision: 1 })
      await running
      assert.deepEqual(plain(h.state.rows.value), before.rows); assert.deepEqual(plain(h.state.pending.value), before.pending)
      assert.deepEqual(h.events, before.events); assert.equal(h.state.failure.value, before.failure)
      assert.equal(reads(h).length, 1)
    } finally { h.dispose() }
  }
})

test('busy guards prevent duplicate writes and navigation, while a pending decision needs explicit leave confirmation', async () => {
  const h = component(), server = fixture(h), gate = deferred()
  try {
    await h.mount(); h.state.decide(h.state.rows.value[0], true)
    h.setConfirm(false); assert.equal(h.state.canLeave(), false)
    h.setConfirm(true); assert.equal(h.state.canLeave(), true)
    assert.equal(h.state.pending.value.phase, 'confirm')
    h.network.request = call => call.method === 'PUT' ? gate.promise : server.read(call)
    const running = h.state.submit(), confirmationCount = h.confirmations.length
    assert.equal(h.state.busy.value, true); assert.equal(h.state.hasUnsavedChanges(), true); assert.equal(h.state.canLeave(), false)
    await h.state.submit(); await h.state.load(); await h.state.refreshSaved(); await h.state.abandon(); h.state.cancel(); h.state.decide(candidate('unrelated'), false)
    assert.equal(writes(h).length, 1); assert.equal(reads(h).length, 1)
    assert.equal(h.confirmations.length, confirmationCount)
    gate.resolve({}); await running
    assert.equal(h.state.busy.value, false); assert.equal(h.state.hasUnsavedChanges(), false); assert.equal(h.state.canLeave(), true)
  } finally { h.dispose() }
})
