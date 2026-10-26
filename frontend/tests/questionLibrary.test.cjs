const assert = require('node:assert/strict')
const { readFileSync } = require('node:fs')
const { resolve } = require('node:path')
const { test: nodeTest } = require('node:test')
const vm = require('node:vm')
const { webcrypto } = require('node:crypto')
const ts = require('typescript')
const { parse } = require('@vue/compiler-sfc')
const vue = require('vue')

const root = resolve(__dirname, '..')
const compile = source => ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
}).outputText
const plain = value => JSON.parse(JSON.stringify(value))
const test = (name, run) => nodeTest(name, { timeout: 5000 }, run)
const headersPlain = headers => Object.fromEntries(new Headers(headers))
const deferred = () => {
  let resolve, reject
  const promise = new Promise((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}
class ApiError extends Error {
  constructor(status, code, message, fieldErrors = []) {
    super(message); this.status = status; this.code = code; this.fieldErrors = fieldErrors
  }
}

// Run the real script setup with actual Vue reactivity. Only HTTP, native
// confirmations and lifecycle delivery are controlled; business logic is not
// copied into these assertions. Rendered controls are checked in the browser.
function component(mode = 'manage', initialProps = {}) {
  const source = parse(readFileSync(resolve(root, 'src/components/QuestionLibrary.vue'), 'utf8')).descriptor.scriptSetup.content
  const exposed = 'busy,failure,note,entries,listOffset,listMore,selectedRoot,history,historyOffset,historyMore,selectedVersionId,selectedVersion,editor,editBaseline,editParentVersionId,editExpectedHeadId,pendingWrite,refreshNeeded,conflictNeedsReview,usageScope,usageVersion,usageRows,usageNext,usageLoaded,loadQuestions,openQuestion,startCreate,startEdit,selectVersion,saveContent,restoreVersion,retryWrite,refreshSaved,reviewConflict,abandonPending,loadMoreHistory,loadUsage,chooseVersion,canLeave'
  const mounted = [], unmounted = [], events = [], confirmations = [], windowEvents = new Map()
  let token = 'account-A-token', confirmResult = true
  const props = vue.reactive({ mode, ...initialProps })
  const network = {
    request: async path => { throw new Error(`Unexpected read: ${path}`) },
    post: async path => { throw new Error(`Unexpected write: ${path}`) },
  }
  const api = { ApiError, getToken: () => token,
    request: (...args) => network.request(...args), post: (...args) => network.post(...args) }
  const modules = new Map()
  const effect = vue.effectScope()
  const context = {
    crypto: webcrypto, URLSearchParams, Headers, setTimeout, clearTimeout,
    confirm: message => { confirmations.push(message); return confirmResult },
    window: { addEventListener: (name, callback) => windowEvents.set(name, callback),
      removeEventListener: name => windowEvents.delete(name) },
    document: { getElementById: () => null },
  }
  function loadModule(filename) {
    if (modules.has(filename)) return modules.get(filename).exports
    const module = { exports: {} }
    modules.set(filename, module)
    vm.runInNewContext(compile(readFileSync(filename, 'utf8')), {
      ...context, module, exports: module.exports,
      require: name => resolveImport(name, filename),
    })
    return module.exports
  }
  function resolveImport(name, filename) {
    if (name === 'vue') return { ...vue,
      onMounted: callback => mounted.push(callback), onUnmounted: callback => unmounted.push(callback),
      onBeforeUnmount: callback => unmounted.push(callback) }
    if (/\/api$/.test(name)) return api
    if (name.endsWith('.vue')) return {}
    if (name.startsWith('.')) return loadModule(resolve(filename, '..', `${name}.ts`))
    throw new Error(`Unexpected import: ${name}`)
  }
  const filename = resolve(root, 'src/components/QuestionLibrary.vue')
  const module = { exports: {} }
  effect.run(() => vm.runInNewContext(compile(source + `\nmodule.exports={${exposed}}`), {
    ...context, module, exports: module.exports, require: name => resolveImport(name, filename),
    defineProps: () => props, withDefaults: value => value,
    defineEmits: () => (name, ...args) => events.push({ name, args: plain(args) }),
    defineExpose: () => {},
  }))
  let alive = true
  return { state: module.exports, props, network, events, confirmations, windowEvents,
    setToken: value => { token = value }, setConfirm: value => { confirmResult = value },
    mount: () => Promise.all(mounted.map(callback => callback())),
    dispose: () => { if (alive) { alive = false; unmounted.forEach(callback => callback()); effect.stop() } },
  }
}

const content = (title, kind = 'number') => ({ kind, title, config: kind === 'number'
  ? { min: 0, max: 10, integerOnly: true } : kind === 'text' ? { minLength: null, maxLength: 100 }
    : { options: [{ id: 'yes', label: '是' }, { id: 'no', label: '否' }],
      ...(kind === 'multi' ? { minSelected: 0, maxSelected: 2 } : {}) } })
const version = (questionId, number, value = content(`内容 ${questionId} V${number}`)) => ({
  id: `${questionId}-v${number}`, questionId, versionNo: number,
  parentVersionId: number === 1 ? null : `${questionId}-v${number - 1}`, content: value,
  createdAt: '2026-10-25T00:00:00Z',
})
function fixture(h) {
  const roots = new Map(), versions = new Map(), reads = []
  function add(questionId = 'q-a') {
    versions.set(questionId, [version(questionId, 2), version(questionId, 1)])
    roots.set(questionId, { id: questionId, currentVersionId: `${questionId}-v2`, lastVersionNo: 2,
      createdAt: '2026-10-25T00:00:00Z', updatedAt: '2026-10-25T00:00:00Z' })
  }
  function commit(saved) {
    versions.set(saved.questionId, [plain(saved), ...(versions.get(saved.questionId) || [])])
    roots.set(saved.questionId, { ...(roots.get(saved.questionId) || { id: saved.questionId,
      createdAt: saved.createdAt }), currentVersionId: saved.id, lastVersionNo: saved.versionNo,
      updatedAt: saved.createdAt })
  }
  add('q-a'); add('q-b')
  h.network.request = async path => {
    reads.push(path)
    const url = new URL(`http://test${path}`), parts = url.pathname.split('/').filter(Boolean)
    const offset = Number(url.searchParams.get('offset') || 0), limit = Number(url.searchParams.get('limit') || 30)
    if (parts.length === 1 && parts[0] === 'questions') return { items: plain([...roots.values()].slice(offset, offset + limit)), offset, limit }
    const id = parts[1]
    if (parts.length === 2 && roots.has(id)) return plain(roots.get(id))
    if (parts[2] === 'versions' && parts.length === 3) return { items: plain((versions.get(id) || []).slice(offset, offset + limit)), offset, limit }
    if (parts[2] === 'versions' && parts.length === 4) {
      const saved = (versions.get(id) || []).find(item => item.id === parts[3])
      assert.ok(saved, path); return plain(saved)
    }
    if (parts[2] === 'usages') return { questionId: id, questionVersionId: url.searchParams.get('questionVersionId'),
      scope: url.searchParams.get('scope') || 'draft', limit, items: [], nextAfterId: null }
    throw new Error(`Unexpected fixture read: ${path}`)
  }
  return { roots, versions, reads, add, commit }
}

async function selected(h, id = 'q-a') {
  await h.state.loadQuestions()
  await h.state.openQuestion(id)
  assert.equal(h.state.selectedRoot.value.id, id)
  assert.equal(h.state.selectedVersion.value.id, `${id}-v2`)
}

test('saved versions are chosen explicitly and picker mode cannot create or edit content', async () => {
  const h = component('pick'), data = fixture(h)
  try {
    await selected(h)
    await h.state.selectVersion('q-a-v1')
    assert.equal(h.state.selectedVersion.value.id, 'q-a-v1')
    h.state.chooseVersion()
    const choices = h.events.filter(item => item.name === 'choose')
    assert.equal(choices.length, 1)
    assert.deepEqual(choices[0].args[0], data.versions.get('q-a')[1])
    const reads = data.reads.length
    await h.state.startCreate('number'); await h.state.startEdit()
    await h.state.saveContent(); await h.state.restoreVersion()
    assert.equal(h.state.editor.value, null)
    assert.equal(data.reads.length, reads)
  } finally { h.dispose() }
})

test('a version switch picker opens its original question and explicitly reads a missing historical version', async () => {
  const h = component('pick', { questionId: 'q-a', versionId: 'q-a-v1' }), data = fixture(h)
  try {
    const original = h.network.request
    h.network.request = async path => path.startsWith('/questions/q-a/versions?')
      ? { items: [plain(data.versions.get('q-a')[0])], offset: 0, limit: 30 }
      : original(path)
    await h.state.loadQuestions()
    assert.equal(h.state.selectedRoot.value.id, 'q-a')
    assert.equal(h.state.selectedVersion.value.id, 'q-a-v1')
    assert.ok(data.reads.includes('/questions/q-a/versions/q-a-v1'))
    assert.ok(!data.reads.some(path => path.startsWith('/questions?')))
    const before = data.reads.length
    await h.state.openQuestion('q-b')
    assert.equal(data.reads.length, before)
    assert.equal(h.state.selectedRoot.value.id, 'q-a')
    h.state.chooseVersion()
    assert.equal(h.events.find(item => item.name === 'choose').args[0].id, 'q-a-v1')
  } finally { h.dispose() }
})

test('editing a historical version binds its source while retaining the separately read current head', async () => {
  const h = component(), data = fixture(h), writes = []
  try {
    await selected(h)
    await h.state.selectVersion('q-a-v1'); await h.state.startEdit()
    h.state.editor.value.title = '从旧版形成新的题干'
    h.network.post = async (path, body, headers) => {
      writes.push({ path, body: plain(body), headers: headersPlain(headers) })
      assert.equal(path, '/questions/q-a/versions')
      assert.equal(body.parentVersionId, 'q-a-v1')
      assert.equal(body.expectedCurrentVersionId, 'q-a-v2')
      assert.equal(body.content.title, '从旧版形成新的题干')
      const saved = { ...version('q-a', 3, plain(body.content)), parentVersionId: 'q-a-v1' }
      data.commit(saved); return plain(saved)
    }
    await h.state.saveContent()
    assert.equal(writes.length, 1)
    assert.ok(writes[0].headers['idempotency-key'])
    assert.equal(h.state.selectedRoot.value.currentVersionId, 'q-a-v3')
    assert.equal(h.state.selectedVersion.value.parentVersionId, 'q-a-v1')
    assert.equal(h.state.pendingWrite.value, null)
  } finally { h.dispose() }
})

for (const kind of ['single', 'multi', 'text', 'number']) {
  test(`creating a ${kind} question sends only reusable content and then selects its saved version`, async () => {
    const h = component(), data = fixture(h), writes = []
    try {
      await h.state.startCreate(kind)
      h.state.editor.value.title = `新建${kind}题目`
      h.network.post = async (path, body, headers) => {
        writes.push({ path, body: plain(body), headers: headersPlain(headers) })
        assert.equal(path, '/questions')
        assert.deepEqual(Object.keys(body), ['content'])
        assert.deepEqual(Object.keys(body.content).sort(), ['config', 'kind', 'title'])
        assert.equal(body.content.kind, kind)
        const saved = version(`created-${kind}`, 1, plain(body.content))
        data.commit(saved); return saved
      }
      await h.state.saveContent()
      assert.equal(writes.length, 1)
      assert.ok(writes[0].headers['idempotency-key'])
      assert.equal(h.state.selectedRoot.value.id, `created-${kind}`)
      assert.equal(h.state.selectedVersion.value.content.kind, kind)
    } finally { h.dispose() }
  })
}

test('an uncertain create retries the original key and body and holds busy through readback', async () => {
  const h = component(), data = fixture(h), writes = []
  try {
    await h.state.startCreate('number')
    h.state.editor.value.title = '本周运动次数'
    h.network.post = async (path, body, headers) => {
      writes.push({ path, body: plain(body), headers: headersPlain(headers) })
      throw new ApiError(0, 'NETWORK_ERROR', '结果尚未确认')
    }
    await h.state.saveContent()
    assert.equal(writes.length, 1)
    assert.ok(h.state.pendingWrite.value)
    const write = deferred(), read = deferred(), readStarted = deferred()
    h.network.post = async (path, body, headers) => {
      writes.push({ path, body: plain(body), headers: headersPlain(headers) })
      return write.promise
    }
    const originalRead = h.network.request
    h.network.request = async path => {
      if (path === '/questions/q-created') { readStarted.resolve(); return read.promise }
      return originalRead(path)
    }
    const running = h.state.retryWrite()
    assert.equal(h.state.busy.value, true)
    await h.state.retryWrite(); await h.state.saveContent()
    assert.equal(writes.length, 2, 'busy retries do not start another write')
    const saved = version('q-created', 1, writes[0].body.content)
    data.commit(saved); write.resolve(saved); await readStarted.promise
    assert.equal(h.state.busy.value, true)
    assert.deepEqual(writes[1], writes[0])
    read.resolve(plain(data.roots.get('q-created'))); await running
    assert.equal(h.state.busy.value, false)
    assert.equal(h.state.pendingWrite.value, null)
    assert.equal(h.state.selectedRoot.value.id, 'q-created')
  } finally { h.dispose() }
})

test('a successful write followed by failed refresh is refreshed without sending another write', async () => {
  const h = component(), data = fixture(h), writes = []
  try {
    await selected(h); await h.state.startEdit()
    h.state.editor.value.title = '已经保存的新内容'
    const originalRead = h.network.request
    let failRefresh = true
    h.network.post = async (path, body, headers) => {
      writes.push({ path, body: plain(body), headers: headersPlain(headers) })
      const saved = version('q-a', 3, plain(body.content)); data.commit(saved); return saved
    }
    h.network.request = async path => {
      if (path === '/questions/q-a' && failRefresh) throw new ApiError(503, 'DATABASE_UNAVAILABLE', '读取失败')
      return originalRead(path)
    }
    await h.state.saveContent()
    assert.equal(writes.length, 1)
    assert.equal(h.state.refreshNeeded.value, true)
    assert.equal(h.state.pendingWrite.value.result.id, 'q-a-v3')
    failRefresh = false
    await h.state.refreshSaved()
    assert.equal(writes.length, 1)
    assert.equal(h.state.refreshNeeded.value, false)
    assert.equal(h.state.selectedVersion.value.id, 'q-a-v3')
  } finally { h.dispose() }
})

test('restoring an old version uses target and current identifiers without client supplied content', async () => {
  const h = component(), data = fixture(h), writes = []
  try {
    await selected(h); await h.state.selectVersion('q-a-v1')
    h.network.post = async (path, body, headers) => {
      writes.push({ path, body: plain(body), headers: headersPlain(headers) })
      assert.equal(path, '/questions/q-a/restore')
      assert.deepEqual(plain(body), { targetVersionId: 'q-a-v1', expectedCurrentVersionId: 'q-a-v2' })
      const saved = { ...version('q-a', 3, data.versions.get('q-a')[1].content),
        parentVersionId: 'q-a-v1', operation: 'restore', previousCurrentVersionId: 'q-a-v2' }
      data.commit(saved); return plain(saved)
    }
    await h.state.restoreVersion()
    assert.equal(writes.length, 1)
    assert.ok(writes[0].headers['idempotency-key'])
    assert.equal(h.state.selectedVersion.value.operation, 'restore')
    assert.equal(h.state.selectedVersion.value.previousCurrentVersionId, 'q-a-v2')
  } finally { h.dispose() }
})

test('an unconfirmed restore result is retried with the original restore key and request body', async () => {
  const h = component(), data = fixture(h), writes = []
  try {
    await selected(h); await h.state.selectVersion('q-a-v1')
    h.network.post = async (path, body, headers) => {
      writes.push({ path, body: plain(body), headers: headersPlain(headers) })
      throw new ApiError(503, 'QUESTION_WRITE_OUTCOME_UNKNOWN', '恢复结果未确认')
    }
    await h.state.restoreVersion()
    assert.equal(writes.length, 1)
    h.network.post = async (path, body, headers) => {
      writes.push({ path, body: plain(body), headers: headersPlain(headers) })
      const saved = { ...version('q-a', 3, data.versions.get('q-a')[1].content),
        parentVersionId: 'q-a-v1', operation: 'restore', previousCurrentVersionId: 'q-a-v2' }
      data.commit(saved); return saved
    }
    await h.state.retryWrite()
    assert.equal(writes.length, 2)
    assert.deepEqual(writes[1], writes[0])
    assert.equal(h.state.selectedVersion.value.operation, 'restore')
  } finally { h.dispose() }
})

test('a version conflict preserves local edits and blocks blind resubmission until explicit review', async () => {
  const h = component(), data = fixture(h), writes = []
  try {
    await selected(h); await h.state.startEdit()
    h.state.editor.value.title = '本页尚未保存的编辑'
    h.network.post = async (path, body, headers) => {
      writes.push({ path, body: plain(body), headers: headersPlain(headers) })
      throw new ApiError(409, 'QUESTION_VERSION_CONFLICT', '当前版本已经变化')
    }
    await h.state.saveContent()
    assert.equal(writes.length, 1)
    assert.equal(h.state.conflictNeedsReview.value, true)
    assert.equal(h.state.editor.value.title, '本页尚未保存的编辑')
    await h.state.saveContent()
    assert.equal(writes.length, 1)
    assert.equal(h.state.editExpectedHeadId.value, 'q-a-v2')
    assert.ok(data.reads.length)
  } finally { h.dispose() }
})

test('reviewing a changed head preserves local content and requires another explicit save with a new key', async () => {
  const h = component(), data = fixture(h), writes = []
  try {
    await selected(h); await h.state.selectVersion('q-a-v1'); await h.state.startEdit()
    h.state.editor.value.title = '继续保留的本地题干'
    h.network.post = async (path, body, headers) => {
      writes.push({ path, body: plain(body), headers: headersPlain(headers) })
      data.commit(version('q-a', 3, content('其他页面已经提交的内容')))
      throw new ApiError(409, 'QUESTION_VERSION_CONFLICT', '其他页面已创建V3')
    }
    await h.state.saveContent()
    assert.equal(writes.length, 1)
    assert.equal(h.state.conflictNeedsReview.value, true)
    h.state.editor.value.title = '冲突后继续修改的本地题干'
    await vue.nextTick()
    assert.ok(h.state.pendingWrite.value, 'the pending conflict keeps its review action after editing')
    assert.equal(h.state.conflictNeedsReview.value, true)
    await h.state.reviewConflict()
    assert.equal(writes.length, 1, 'review does not silently submit the preserved edits')
    assert.equal(h.state.conflictNeedsReview.value, false)
    assert.equal(h.state.editor.value.title, '冲突后继续修改的本地题干')
    assert.equal(h.state.editParentVersionId.value, 'q-a-v1')
    assert.equal(h.state.editExpectedHeadId.value, 'q-a-v3')
    h.network.post = async (path, body, headers) => {
      writes.push({ path, body: plain(body), headers: headersPlain(headers) })
      assert.equal(body.parentVersionId, 'q-a-v1')
      assert.equal(body.expectedCurrentVersionId, 'q-a-v3')
      assert.equal(body.content.title, '冲突后继续修改的本地题干')
      const saved = { ...version('q-a', 4, plain(body.content)), parentVersionId: 'q-a-v1' }
      data.commit(saved); return saved
    }
    await h.state.saveContent()
    assert.equal(writes.length, 2)
    assert.notEqual(writes[1].headers['idempotency-key'], writes[0].headers['idempotency-key'])
    assert.equal(h.state.selectedVersion.value.id, 'q-a-v4')
  } finally { h.dispose() }
})

test('usage queries bind the selected version and preserve their scope for later pages', async () => {
  const h = component(), data = fixture(h), requests = []
  try {
    await selected(h); await h.state.selectVersion('q-a-v1')
    h.state.usageScope.value = 'published'; h.state.usageVersion.value = 'selected'
    const original = h.network.request
    h.network.request = async path => {
      if (!path.includes('/usages?')) return original(path)
      requests.push(path)
      const params = new URL(`http://test${path}`).searchParams
      assert.equal(params.get('scope'), 'published')
      assert.equal(params.get('questionVersionId'), 'q-a-v1')
      return { questionId: 'q-a', questionVersionId: 'q-a-v1', scope: 'published', limit: 20,
        items: [{ surveyId: requests.length === 1 ? 'survey-1' : 'survey-2',
          surveyVersionId: requests.length === 1 ? 'publication-1' : 'publication-2',
          surveyTitle: '冻结标题', surveyStatus: 'closed', versionNo: 1, isCurrent: true,
          publishedAt: '2026-10-25T00:00:00Z', instances: [{ instanceId: 'score', questionVersionId: 'q-a-v1' }] }],
        nextAfterId: requests.length === 1 ? 'publication-1' : null }
    }
    await h.state.loadUsage()
    assert.equal(h.state.usageRows.value.length, 1)
    assert.equal(h.state.usageNext.value, 'publication-1')
    await h.state.loadUsage(true)
    assert.equal(h.state.usageRows.value.length, 2)
    assert.equal(new URL(`http://test${requests[1]}`).searchParams.get('afterId'), 'publication-1')
    assert.equal(h.state.usageNext.value, null)
    assert.ok(data.reads.length)
  } finally { h.dispose() }
})

test('dirty content blocks leaving or switching questions when native confirmation is declined', async () => {
  const h = component(), data = fixture(h)
  try {
    await selected(h); await h.state.startEdit()
    h.state.editor.value.title = '还没有保存'
    h.setConfirm(false)
    assert.equal(h.state.canLeave(), false)
    const before = data.reads.length
    await h.state.openQuestion('q-b')
    assert.equal(h.state.selectedRoot.value.id, 'q-a')
    assert.equal(h.state.editor.value.title, '还没有保存')
    assert.equal(data.reads.length, before)
    assert.ok(h.confirmations.length)
  } finally { h.dispose() }
})

for (const invalidate of ['token-change', 'unmount']) {
  test(`a late read after ${invalidate} cannot replace selection or emit a choice`, async () => {
    const h = component(), data = fixture(h), read = deferred(), started = deferred()
    try {
      await selected(h)
      const original = h.network.request
      h.network.request = async path => {
        if (path === '/questions/q-b') { started.resolve(); return read.promise }
        return original(path)
      }
      const running = h.state.openQuestion('q-b'); await started.promise
      if (invalidate === 'token-change') h.setToken('account-B-token')
      else h.dispose()
      const selectedBefore = plain(h.state.selectedRoot.value)
      const eventCount = h.events.length
      read.resolve(plain(data.roots.get('q-b'))); await running
      assert.deepEqual(plain(h.state.selectedRoot.value), selectedBefore)
      assert.equal(h.events.slice(eventCount).filter(item => item.name === 'choose').length, 0)
    } finally { read.resolve(plain(data.roots.get('q-b'))); h.dispose() }
  })
}

test('an authorization failure notifies the parent only for the still current account', async () => {
  const h = component(), data = fixture(h)
  try {
    await selected(h)
    h.network.request = async () => { throw new ApiError(401, 'UNAUTHENTICATED', '登录失效') }
    await h.state.loadQuestions()
    assert.equal(h.events.filter(item => item.name === 'unauthorized').length, 1)
    const late = deferred(), started = deferred()
    h.network.request = async () => { started.resolve(); return late.promise }
    const running = h.state.loadQuestions(); await started.promise
    h.setToken('new-account-token')
    late.reject(new ApiError(401, 'UNAUTHENTICATED', '旧账号迟到失败')); await running
    assert.equal(h.events.filter(item => item.name === 'unauthorized').length, 1)
    assert.ok(data.reads.length)
  } finally { h.dispose() }
})
