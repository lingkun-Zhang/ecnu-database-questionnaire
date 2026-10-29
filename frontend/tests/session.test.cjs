const assert = require('node:assert/strict')
const { readFileSync } = require('node:fs')
const { resolve } = require('node:path')
const { test } = require('node:test')
const vm = require('node:vm')
const { webcrypto } = require('node:crypto')
const ts = require('typescript')
const { parse } = require('@vue/compiler-sfc')
const vue = require('vue')

const root = resolve(__dirname, '..')
const compile = source => ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
}).outputText
const appSource = parse(readFileSync(resolve(root, 'src/App.vue'), 'utf8')).descriptor.scriptSetup.content
const typesModule = { exports: {} }
vm.runInNewContext(compile(readFileSync(resolve(root, 'src/types.ts'), 'utf8')), {
  module: typesModule, exports: typesModule.exports, crypto: webcrypto,
})
const editorErrorsModule = { exports: {} }
vm.runInNewContext(compile(readFileSync(resolve(root, 'src/editorErrors.ts'), 'utf8')), {
  module: editorErrorsModule, exports: editorErrorsModule.exports,
})
const reuseModule = { exports: {} }
vm.runInNewContext(compile(readFileSync(resolve(root, 'src/questionReuse.ts'), 'utf8')), {
  module: reuseModule, exports: reuseModule.exports,
  require: name => { if (name === './types') return typesModule.exports; throw new Error(name) },
})

function storage() {
  const values = new Map()
  return { getItem: key => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, String(value)), removeItem: key => values.delete(key) }
}
function deferred() {
  let resolve, reject
  const promise = new Promise((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}
class ApiError extends Error {
  constructor(status, code, message) { super(message); this.status = status; this.code = code; this.fieldErrors = [] }
}

// Execute the component's real setup code with Vue refs/computed and controlled
// HTTP boundaries. No browser state or running application data is modified.
function component(pathname = '/', sharedStorage = {}) {
  const localStorage = sharedStorage.localStorage || storage()
  const sessionStorage = sharedStorage.sessionStorage || storage()
  const events = new Map()
  let mounted
  const network = {
    request: async path => {
      if (path.startsWith('/surveys?')) return { items: [] }
      if (path.startsWith('/survey/')) return { title: 'public form' }
      throw new Error(`Unexpected request: ${path}`)
    },
    post: async path => {
      assert.equal(path, '/auth/login')
      return { accessToken: 'token-B', user: { id: 'B', username: 'account_B' } }
    },
  }
  const api = { ApiError, getToken: () => localStorage.getItem('wenchuan_token') || '',
    setToken: token => token ? localStorage.setItem('wenchuan_token', token) : localStorage.removeItem('wenchuan_token'),
    request: (...args) => network.request(...args), post: (...args) => network.post(...args), download: async () => {} }
  const module = { exports: {} }
  const exposed = 'user,page,active,definition,savedDefinition,dirty,editorTab,editorFieldErrors,currentDesignWarnings,definitionEditor,focusEditorIssue,focusDesignWarning,runPreview,previewResult,stats,statsVersion,statsQuestion,statsRespondentType,detailItems,nextDetailCursor,loadDetails,exportCSV,openSurvey,versions,showHistory,receipt,pending,error,notice,busy,username,password,authenticate,logout,perform,refreshStats,submitFill,rememberPending,sessionChanged,fillDefinition,fillVersionId,fillToken,fillAnswers,anonymous,publicInfo,availabilityMessage,startFill,closeConfirmation,requestCloseSurvey,closeSurvey,escapeDialog,saveDraft,publish,goDashboard,createSurvey,showStats,settings,openHistory,copyVersion,reloadCurrent,deadlineInput,allowMultipleInput,questionLibrary,questionBusy,questionPicker,localQuestionSources,currentQuestionSources,openQuestions,openQuestionPicker,chooseQuestionVersion,detachQuestion,beforeUnload,expireQuestionSession,remoteDraftChanged,questionBanks,pickerLibrary,pickerBanks,pickerCollection,openBanks,changePickerCollection,closeQuestionPicker,questionStatistics,statQuestionId,statisticsGrants,questionWork,openQuestionStatistics,returnToEditor,changeEditorTab,openSourceQuestionStatistics,versionDetails,nextVersionOffset,loadMoreVersions,readVersion,historyComparisonReady,historyLeft,historyRight,leftVersionId,rightVersionId,differences,compareHistory,viewVersion'
  vm.runInNewContext(compile(appSource + `\nmodule.exports={${exposed}}`), {
    module, exports: module.exports,
    require: name => {
      if (name === 'vue') return { ...vue, onMounted: callback => { mounted = callback }, onUnmounted: () => {} }
      if (name === './api') return api
      if (name === './types') return typesModule.exports
      if (name === './editorErrors') return editorErrorsModule.exports
      if (name === './questionReuse') return reuseModule.exports
      throw new Error(`Unexpected import: ${name}`)
    },
    location: { pathname, origin: 'http://localhost' }, localStorage, sessionStorage,
    window: { addEventListener: (name, handler) => events.set(name, handler), removeEventListener: name => events.delete(name) },
    document: { getElementById: () => null },
    navigator: { clipboard: { writeText: async () => {} } }, confirm: sharedStorage.confirm || (() => true),
    URLSearchParams, crypto: webcrypto, setTimeout, clearTimeout,
  })
  const state = module.exports
  function seedOwner() {
    api.setToken('token-A'); state.user.value = { id: 'A', username: 'account_A' }
    state.page.value = 'statistics'; state.active.value = { id: 'private-survey' }
    state.statsVersion.value = 'private-version'; state.stats.value = { totalResponses: 1 }
    state.detailItems.value = [{ respondentName: 'private-person' }]
    state.versions.value = [{ id: 'private-version' }]; state.showHistory.value = true
  }
  async function loginB() {
    state.username.value = 'account_B'; state.password.value = 'test-only'
    await state.authenticate()
    assert.equal(state.user.value?.id, 'B'); assert.equal(api.getToken(), 'token-B')
  }
  return { state, api, network, localStorage, sessionStorage, events, mount: () => mounted(), seedOwner, loginB }
}

const plain = value => JSON.parse(JSON.stringify(value))

test('switching only a source version is dirty and saves the explicit mapping with the same content', async () => {
  const h = component(), original = seedEditor(h, false), question = plain(h.state.definition.value.questions[0])
  const oldSource = { instanceId: question.id, questionId: 'bank', questionVersionId: 'bank-v1' }
  h.state.active.value.draftQuestionSources = [oldSource]
  h.network.post = async path => { assert.match(path, /\/preview$/); return { path: [], errors: [], warnings: [] } }
  h.state.openQuestionPicker(question.id)
  h.state.chooseQuestionVersion({ id: 'bank-v2', questionId: 'bank', versionNo: 2, content: { kind: question.kind, title: question.title, config: question.config } })
  assert.equal(h.state.dirty.value, true)
  assert.deepEqual(plain(h.state.definition.value), original.draft)
  const sources = [{ ...oldSource, questionVersionId: 'bank-v2' }]
  const refreshed = { ...original, draftRevision: 5, draftQuestionSources: sources }
  let writes = 0
  h.network.request = async (path, options) => {
    if (path.endsWith('/draft')) { writes++; assert.deepEqual(JSON.parse(options.body).questionSources, sources); return refreshed }
    if (path.includes('/versions?')) return { items: [] }
    return refreshed
  }
  await h.state.saveDraft()
  assert.equal(writes, 1); assert.equal(h.state.dirty.value, false)
  assert.equal(h.state.localQuestionSources.value, null)
  h.state.logout()
})

test('detaching a source marks unchanged content dirty and sends an explicit empty list', async () => {
  const h = component(), original = seedEditor(h, false), id = original.draft.questions[0].id
  h.state.active.value.draftQuestionSources = [{ instanceId: id, questionId: 'bank', questionVersionId: 'v1' }]
  h.state.detachQuestion(id)
  assert.equal(h.state.dirty.value, true)
  assert.deepEqual(plain(h.state.definition.value), original.draft)
  h.network.request = async (path, options) => {
    if (path.endsWith('/draft')) { assert.deepEqual(JSON.parse(options.body).questionSources, []); return { ...original, draftRevision: 5 } }
    return path.includes('/versions?') ? { items: [] } : { ...original, draftRevision: 5 }
  }
  await h.state.saveDraft(); assert.equal(h.state.dirty.value, false); h.state.logout()
})

test('a picker opened for another survey or an expired account cannot apply its version', () => {
  const h = component(); seedEditor(h, false)
  h.state.openQuestionPicker(); h.state.active.value = { ...h.state.active.value, id: 'different-survey' }
  const before = JSON.stringify(h.state.definition.value), saved = { id: 'v1', questionId: 'bank', versionNo: 1, content: { kind: 'text', title: '迟到选题', config: {} } }
  h.state.chooseQuestionVersion(saved); assert.equal(JSON.stringify(h.state.definition.value), before)
  h.state.logout(); h.state.chooseQuestionVersion(saved)
  assert.equal(h.state.definition.value, null); assert.equal(h.state.questionPicker.value, null)
})

test('source validation errors locate the editor and expire when provenance is explicitly detached', async () => {
  const h = component(), original = seedEditor(h, false), id = original.draft.questions[0].id
  h.state.active.value.draftQuestionSources = [{ instanceId: id, questionId: 'bank', questionVersionId: 'v1' }]
  const failure = new ApiError(422, 'INVALID_QUESTION_SOURCE', '内容不一致')
  failure.fieldErrors = [{ questionId: id, field: 'questions[0].config', message: '内容不一致' }]
  h.network.request = async () => { throw failure }
  await h.state.saveDraft(); assert.equal(h.state.editorFieldErrors.value.length, 1)
  let focused = false
  h.state.definitionEditor.value = { focusError: async issue => { focused = issue === h.state.error.value.fieldErrors[0] } }
  await h.state.focusEditorIssue(h.state.error.value.fieldErrors[0]); assert.equal(focused, true)
  h.state.detachQuestion(id); assert.equal(h.state.error.value, null); assert.equal(h.state.editorFieldErrors.value.length, 0)
  h.state.logout()
})

test('personal-question pending work protects navigation and page unload', async () => {
  const h = component(); h.seedOwner(); h.state.page.value = 'questions'
  h.state.questionLibrary.value = { canLeave: () => false, hasUnsavedChanges: () => true }
  h.network.request = async () => { throw new Error('Navigation must not request data') }
  await h.state.goDashboard(); await h.state.openSurvey('another')
  assert.equal(h.state.page.value, 'questions')
  let prevented = false; const event = { preventDefault: () => { prevented = true } }
  h.state.beforeUnload(event); assert.equal(prevented, true)
  h.state.logout()
})

test('an expired library session clears credentials even when manual logout confirmation is declined', () => {
  const h = component('/', { confirm: () => false }); h.seedOwner(); h.state.page.value = 'questions'
  h.state.questionBusy.value = true
  h.state.questionLibrary.value = { canLeave: () => false, hasUnsavedChanges: () => true }
  h.state.logout(); assert.equal(h.api.getToken(), 'token-A')
  h.state.expireQuestionSession()
  assert.equal(h.api.getToken(), ''); assert.equal(h.state.user.value, null)
  assert.equal(h.state.questionBusy.value, false); assert.equal(h.state.error.value.status, 401)
})

test('settings refresh preserves the local draft revision and sources when another page has saved a new version', async () => {
  const h = component(), original = seedEditor(h, false), id = original.draft.questions[0].id
  const oldSources = [{ instanceId: id, questionId: 'bank', questionVersionId: 'v1' }]
  h.state.active.value.draftQuestionSources = oldSources
  const remote = { ...plain(h.state.active.value), draftRevision: 5, settingsRevision: 3,
    draftQuestionSources: [{ ...oldSources[0], questionVersionId: 'v2' }], draft: { ...original.draft, title: '其他页面的新草稿' } }
  h.network.request = async (path, options) => options?.method === 'PATCH' ? remote : remote
  await h.state.settings()
  assert.equal(h.state.active.value.settingsRevision, 3)
  assert.equal(h.state.active.value.draftRevision, 4)
  assert.deepEqual(plain(h.state.currentQuestionSources.value), oldSources)
  assert.deepEqual(plain(h.state.definition.value), original.draft)
  assert.equal(h.state.remoteDraftChanged.value, true)
  h.network.post = async () => { throw new Error('An unseen remote draft must not be published') }
  await h.state.publish(); h.state.logout()
})

function seedEditor(h, changed = true) {
  h.seedOwner()
  const original = typesModule.exports.exampleDefinition()
  original.title = '服务器上的草稿'
  h.state.page.value = 'editor'; h.state.editorTab.value = 'design'
  h.state.showHistory.value = false; h.state.versions.value = []
  h.state.active.value = { id: 'private-survey', draft: plain(original), draftRevision: 4,
    settingsRevision: 2, currentVersionId: 'version-1', lastVersionNo: 1,
    status: 'open', deadlineAt: null, allowMultiple: false, policyLocked: false }
  h.state.definition.value = plain(original)
  h.state.savedDefinition.value = JSON.stringify(original)
  h.state.deadlineInput.value = ''; h.state.allowMultipleInput.value = false
  if (changed) h.state.definition.value.title = '这次准备保存的标题'
  return plain(h.state.active.value)
}

function editorVersion(definition, versionNo = 1) {
  return { id: `version-${versionNo}`, versionNo, definition: plain(definition),
    sourceDraftRevision: 4, publishedAt: '2026-10-22T00:00:00Z' }
}

// These tests exercise operation boundaries and recovery through the real setup
// code. Native fieldset inheritance is verified separately in a real browser.
for (const operation of ['saveDraft', 'publish']) {
  test(`${operation} stays busy through its write, refreshed draft and first version-summary page`, async () => {
    const h = component(), original = seedEditor(h, operation === 'saveDraft')
    const submitted = plain(h.state.definition.value), write = deferred(), read = deferred(), versionList = deferred()
    const readStarted = deferred(), versionsStarted = deferred(), calls = []
    const refreshed = { ...original, draft: submitted, draftRevision: operation === 'saveDraft' ? 5 : 4,
      currentVersionId: operation === 'publish' ? 'version-2' : 'version-1',
      lastVersionNo: operation === 'publish' ? 2 : 1 }
    h.network.request = async (path, options) => {
      calls.push({ path, options })
      if (path.endsWith('/draft')) {
        assert.equal(options.method, 'PUT')
        assert.deepEqual(JSON.parse(options.body), { definition: submitted, expectedDraftRevision: 4 })
        return write.promise
      }
      if (path === '/surveys/private-survey') { readStarted.resolve(); return read.promise }
      assert.match(path, /^\/surveys\/private-survey\/versions\?/)
      versionsStarted.resolve(); return versionList.promise
    }
    h.network.post = async (path, body) => {
      calls.push({ path, body: plain(body) })
      assert.equal(path, '/surveys/private-survey/publish')
      assert.equal(body.expectedDraftRevision, 4); assert.equal(body.expectedCurrentVersionId, 'version-1')
      return write.promise
    }
    const running = h.state[operation]()
    try {
      assert.equal(h.state.busy.value, true)
      assert.equal(calls.length, 1)
      write.resolve(refreshed); await readStarted.promise
      assert.equal(h.state.busy.value, true, 'reading the saved draft is part of the same operation')
      assert.equal(h.state.active.value.draftRevision, 4)
      read.resolve(refreshed); await versionsStarted.promise
      assert.equal(h.state.busy.value, true, 'the editor remains locked until the version list is current')
      assert.deepEqual(plain(h.state.definition.value), submitted)
      const version = editorVersion(submitted, refreshed.lastVersionNo)
      versionList.resolve({ items: [version] }); await running
      assert.equal(h.state.busy.value, false)
      assert.equal(h.state.error.value, null)
      assert.ok(h.state.notice.value)
      assert.equal(h.state.active.value.draftRevision, refreshed.draftRevision)
      assert.equal(h.state.active.value.currentVersionId, refreshed.currentVersionId)
      assert.deepEqual(plain(h.state.versions.value), [version])
      assert.equal(h.state.savedDefinition.value, JSON.stringify(submitted))
      assert.equal(h.state.dirty.value, false)
    } finally {
      write.resolve(refreshed); read.resolve(refreshed); versionList.resolve({ items: [] })
      await running; h.state.logout()
    }
  })

  test(`${operation} releases a failed write without losing local content and can be retried`, async () => {
    const h = component(), original = seedEditor(h, operation === 'saveDraft')
    const local = plain(h.state.definition.value), savedBefore = h.state.savedDefinition.value
    const write = deferred(), writes = [], failure = new ApiError(503, 'TEMPORARY_NETWORK_FAILURE', '稍后重试')
    h.network.request = async (path, options) => {
      assert.ok(path.endsWith('/draft')); writes.push({ path, body: JSON.parse(options.body) }); return write.promise
    }
    h.network.post = async (path, body) => { writes.push({ path, body: plain(body) }); return write.promise }
    const running = h.state[operation]()
    assert.equal(h.state.busy.value, true)
    write.reject(failure); await running
    assert.equal(h.state.busy.value, false)
    assert.equal(h.state.error.value.code, 'TEMPORARY_NETWORK_FAILURE')
    assert.equal(h.state.notice.value, '')
    assert.deepEqual(plain(h.state.definition.value), local)
    assert.equal(h.state.savedDefinition.value, savedBefore)
    assert.equal(h.state.dirty.value, operation === 'saveDraft')
    assert.deepEqual(plain(h.state.active.value), original)
    const refreshed = { ...original, draft: local, draftRevision: operation === 'saveDraft' ? 5 : 4,
      currentVersionId: operation === 'publish' ? 'version-2' : 'version-1' }
    h.network.request = async (path, options) => {
      if (path.endsWith('/draft')) { writes.push({ path, body: JSON.parse(options.body) }); return refreshed }
      return path.includes('/versions?') ? { items: [editorVersion(local)] } : refreshed
    }
    h.network.post = async (path, body) => { writes.push({ path, body: plain(body) }); return refreshed }
    await h.state[operation]()
    assert.equal(writes.length, 2)
    assert.deepEqual(writes[1], writes[0], 'a failed publish retries its original idempotency key and revision')
    assert.equal(h.state.busy.value, false); assert.equal(h.state.error.value, null)
    assert.equal(h.state.dirty.value, false)
    h.state.logout()
  })

  for (const failingRead of ['draft', 'versions']) {
    test(`${operation} unlocks after its ${failingRead} refresh fails and preserves the correct saved baseline`, async () => {
      const h = component(), original = seedEditor(h, operation === 'saveDraft')
      const local = plain(h.state.definition.value), savedBefore = h.state.savedDefinition.value
      const refresh = deferred(), refreshStarted = deferred()
      const refreshed = { ...original, draft: local, draftRevision: operation === 'saveDraft' ? 5 : 4 }
      h.network.post = async () => refreshed
      h.network.request = async (path) => {
        if (path.endsWith('/draft')) return refreshed
        const isVersionRead = path.includes('/versions?')
        if ((failingRead === 'versions') === isVersionRead) { refreshStarted.resolve(); return refresh.promise }
        return refreshed
      }
      const running = h.state[operation]()
      try {
        await refreshStarted.promise
        assert.equal(h.state.busy.value, true)
        refresh.reject(new ApiError(503, 'REFRESH_FAILED', '刷新失败')); await running
        assert.equal(h.state.busy.value, false)
        assert.equal(h.state.error.value.code, 'REFRESH_FAILED')
        assert.equal(h.state.notice.value, '')
        assert.deepEqual(plain(h.state.definition.value), local)
        assert.equal(h.state.savedDefinition.value, failingRead === 'versions' ? JSON.stringify(local) : savedBefore)
        assert.equal(h.state.dirty.value, failingRead === 'draft' && operation === 'saveDraft')
      } finally {
        refresh.resolve(refreshed); await running; h.state.logout()
      }
    })
  }

  for (const waitingAt of ['write', 'versions']) {
    test(`${operation} cannot restore account A after logout while waiting for ${waitingAt}`, async () => {
      const h = component(), original = seedEditor(h, operation === 'saveDraft')
      const local = plain(h.state.definition.value), gate = deferred(), gateStarted = deferred(), calls = []
      const refreshed = { ...original, draft: local, draftRevision: 5 }
      h.network.post = async path => {
        calls.push(path)
        if (waitingAt === 'write') { gateStarted.resolve(); return gate.promise }
        return refreshed
      }
      h.network.request = async path => {
        calls.push(path)
        if (waitingAt === 'write' && path.endsWith('/draft')) { gateStarted.resolve(); return gate.promise }
        if (waitingAt === 'versions' && path.includes('/versions?')) { gateStarted.resolve(); return gate.promise }
        return refreshed
      }
      const running = h.state[operation]()
      try {
        await gateStarted.promise
        assert.equal(h.state.busy.value, true)
        const callsBeforeLogout = calls.length
        h.state.logout()
        h.network.post = async () => ({ accessToken: 'token-B', user: { id: 'B', username: 'account_B' } })
        h.network.request = async () => ({ items: [] })
        await h.loginB()
        gate.resolve(waitingAt === 'versions' ? { items: [editorVersion(local)] } : refreshed)
        await running
        assert.equal(calls.length, callsBeforeLogout, 'a late write does not start private follow-up reads')
        assert.equal(h.state.user.value.id, 'B'); assert.equal(h.api.getToken(), 'token-B')
        assert.equal(h.state.page.value, 'dashboard'); assert.equal(h.state.active.value, null)
        assert.equal(h.state.definition.value, null); assert.equal(h.state.savedDefinition.value, '')
        assert.deepEqual(plain(h.state.versions.value), [])
        assert.equal(h.state.busy.value, false); assert.equal(h.state.error.value, null)
        assert.equal(h.state.notice.value, '')
      } finally {
        gate.resolve(waitingAt === 'versions' ? { items: [] } : refreshed)
        await running; h.state.logout()
      }
    })
  }
}

test('a busy editor rejects duplicate writes, reload, history actions and navigation without issuing HTTP', async () => {
  const h = component(), original = seedEditor(h, false), calls = []
  const definitionBefore = plain(h.state.definition.value), savedBefore = h.state.savedDefinition.value
  h.state.busy.value = true
  h.network.request = async (path) => { calls.push(path); return path.includes('/versions?') ? { items: [] } : original }
  h.network.post = async (path) => { calls.push(path); return original }
  await Promise.all([
    h.state.saveDraft(), h.state.publish(), h.state.settings(), h.state.reloadCurrent(),
    h.state.openHistory(), h.state.copyVersion(editorVersion(original.draft)),
    h.state.goDashboard(), h.state.openSurvey('other-survey'), h.state.createSurvey(), h.state.showStats(),
  ])
  assert.deepEqual(calls, [])
  assert.equal(h.state.busy.value, true, 'a rejected second operation must not unlock the first one')
  assert.equal(h.state.page.value, 'editor'); assert.equal(h.state.showHistory.value, false)
  assert.deepEqual(plain(h.state.active.value), original)
  assert.deepEqual(plain(h.state.definition.value), definitionBefore)
  assert.equal(h.state.savedDefinition.value, savedBefore)
  h.state.logout()
})

test('collection settings stay busy through metadata refresh and retain the unsaved definition', async () => {
  const h = component(), original = seedEditor(h), write = deferred(), read = deferred(), readStarted = deferred()
  const local = plain(h.state.definition.value), savedBefore = h.state.savedDefinition.value, calls = []
  h.state.deadlineInput.value = '2026-10-29T18:00'; h.state.allowMultipleInput.value = true
  const refreshed = { ...original, settingsRevision: 3, allowMultiple: true, deadlineAt: '2026-10-29T10:00:00Z' }
  h.network.request = async (path, options) => {
    calls.push({ path, options })
    if (path.endsWith('/settings')) {
      assert.equal(options.method, 'PATCH')
      const body = JSON.parse(options.body)
      assert.equal(body.expectedSettingsRevision, 2); assert.equal(body.allowMultiple, true)
      assert.equal(body.deadlineAt, new Date('2026-10-29T18:00').toISOString())
      return write.promise
    }
    assert.equal(path, '/surveys/private-survey'); readStarted.resolve(); return read.promise
  }
  const running = h.state.settings()
  try {
    assert.equal(h.state.busy.value, true)
    write.resolve(refreshed); await readStarted.promise
    assert.equal(h.state.busy.value, true)
    read.resolve(refreshed); await running
    assert.equal(calls.length, 2); assert.equal(h.state.busy.value, false)
    assert.equal(h.state.active.value.settingsRevision, 3)
    assert.equal(h.state.allowMultipleInput.value, true)
    assert.deepEqual(plain(h.state.definition.value), local)
    assert.equal(h.state.savedDefinition.value, savedBefore); assert.equal(h.state.dirty.value, true)
    assert.equal(h.state.error.value, null)
  } finally {
    write.resolve(refreshed); read.resolve(refreshed); await running; h.state.logout()
  }
})

test('copying a historical version stays busy until the refreshed draft is available', async () => {
  const h = component(), original = seedEditor(h), write = deferred(), read = deferred(), readStarted = deferred()
  const historical = plain(original.draft); historical.title = '需要恢复的历史内容'
  const version = editorVersion(historical), refreshed = { ...original, draft: historical, draftRevision: 5 }
  h.state.showHistory.value = true
  const calls = []
  h.network.request = async (path, options) => {
    calls.push({ path, options })
    if (path.endsWith('/draft')) {
      assert.deepEqual(JSON.parse(options.body), { definition: historical, expectedDraftRevision: 4, questionSources: [] })
      return write.promise
    }
    if (path.includes('/versions?')) return { items: [version] }
    assert.equal(path, '/surveys/private-survey'); readStarted.resolve(); return read.promise
  }
  const running = h.state.copyVersion(version)
  try {
    assert.equal(h.state.busy.value, true)
    write.resolve(refreshed); await readStarted.promise
    assert.equal(h.state.busy.value, true)
    assert.notEqual(h.state.definition.value.title, historical.title)
    read.resolve(refreshed); await running
    assert.equal(calls.length, 3); assert.equal(h.state.busy.value, false)
    assert.equal(h.state.showHistory.value, false)
    assert.equal(h.state.page.value, 'editor'); assert.equal(h.state.editorTab.value, 'design')
    assert.deepEqual(plain(h.state.definition.value), historical)
    assert.equal(h.state.dirty.value, false); assert.equal(h.state.error.value, null)
  } finally {
    write.resolve(refreshed); read.resolve(refreshed); await running; h.state.logout()
  }
})

for (const deletedIds of [[], ['q_note'], ['q_age', 'q_note']]) {
  test(`saving a sourced draft keeps only sources for retained instances (${deletedIds.join(',') || 'no deletions'})`, async () => {
    const h = component(), original = seedEditor(h), calls = []
    const sources = [
      { instanceId: 'q_age', questionId: 'library-age', questionVersionId: 'library-age-v1' },
      { instanceId: 'q_note', questionId: 'library-note', questionVersionId: 'library-note-v2' },
    ]
    h.state.active.value.draftQuestionSources = plain(sources)
    h.state.definition.value.questions = h.state.definition.value.questions.filter(q => !deletedIds.includes(q.id))
    const submitted = plain(h.state.definition.value), retained = sources.filter(source => !deletedIds.includes(source.instanceId))
    const refreshed = { ...original, draft: submitted, draftRevision: 5,
      ...(retained.length ? { draftQuestionSources: retained } : {}) }
    h.network.request = async (path, options) => {
      calls.push(path)
      if (path.endsWith('/draft')) {
        assert.equal(options.method, 'PUT')
        assert.deepEqual(JSON.parse(options.body), {
          definition: submitted, expectedDraftRevision: 4, questionSources: retained,
        })
        assert.deepEqual(plain(h.state.active.value.draftQuestionSources), sources, 'filtering the request does not mutate the saved metadata')
        return refreshed
      }
      return path.includes('/versions?') ? { items: [] } : refreshed
    }
    await h.state.saveDraft()
    assert.equal(calls.length, 3)
    assert.equal(h.state.error.value, null); assert.equal(h.state.dirty.value, false)
    assert.deepEqual(plain(h.state.active.value.draftQuestionSources ?? []), retained)
    h.state.logout()
  })
}

test('editing a retained sourced instance preserves its source and local edits when the server rejects mismatched content', async () => {
  const h = component(), original = seedEditor(h), calls = []
  const sources = [{ instanceId: 'q_age', questionId: 'library-age', questionVersionId: 'library-age-v1' }]
  h.state.active.value.draftQuestionSources = plain(sources)
  h.state.definition.value.questions.find(q => q.id === 'q_age').title = '尚未另存为题目版本的修改'
  const submitted = plain(h.state.definition.value), savedBefore = h.state.savedDefinition.value
  h.network.request = async (path, options) => {
    calls.push(path)
    assert.deepEqual(JSON.parse(options.body), {
      definition: submitted, expectedDraftRevision: 4, questionSources: sources,
    })
    throw new ApiError(422, 'INVALID_QUESTION_SOURCE', '题目内容与指定版本不一致')
  }
  await h.state.saveDraft()
  assert.deepEqual(calls, ['/surveys/private-survey/draft'])
  assert.equal(h.state.error.value.code, 'INVALID_QUESTION_SOURCE')
  assert.deepEqual(plain(h.state.definition.value), submitted)
  assert.deepEqual(plain(h.state.active.value), { ...original, draftQuestionSources: sources })
  assert.equal(h.state.savedDefinition.value, savedBefore); assert.equal(h.state.dirty.value, true)
  assert.equal(h.state.busy.value, false); assert.equal(h.state.notice.value, '')
  h.state.logout()
})

for (const hasHistoricalSources of [true, false]) {
  test(`copying history ${hasHistoricalSources ? 'restores its fixed sources' : 'explicitly clears the current sources when history has none'}`, async () => {
    const h = component(), original = seedEditor(h), writes = []
    const currentSources = [{ instanceId: 'q_age', questionId: 'library-age', questionVersionId: 'library-age-v2' }]
    const historicalSources = [{ instanceId: 'q_age', questionId: 'library-age', questionVersionId: 'library-age-v1' }]
    h.state.active.value.draftQuestionSources = plain(currentSources)
    const historical = plain(original.draft); historical.title = '历史问卷与其当时来源'
    const version = { ...editorVersion(historical), ...(hasHistoricalSources ? { questionSources: historicalSources } : {}) }
    const expectedSources = hasHistoricalSources ? historicalSources : []
    const refreshed = { ...original, draft: historical, draftRevision: 5,
      ...(hasHistoricalSources ? { draftQuestionSources: historicalSources } : {}) }
    h.network.request = async (path, options) => {
      if (path.endsWith('/draft')) {
        writes.push(JSON.parse(options.body))
        return refreshed
      }
      return path.includes('/versions?') ? { items: [version] } : refreshed
    }
    await h.state.copyVersion(version)
    assert.deepEqual(writes, [{ definition: historical, expectedDraftRevision: 4, questionSources: expectedSources }])
    assert.deepEqual(plain(h.state.active.value.draftQuestionSources ?? []), expectedSources)
    assert.equal(h.state.error.value, null); assert.equal(h.state.dirty.value, false)
    h.state.logout()
  })
}

for (const [code, status] of [['SURVEY_CLOSED', 'closed'], ['SURVEY_EXPIRED', 'expired']]) {
  test(`submit preflight preserves ${code} and the answer without creating a recovery key or POST submission`, async () => {
    const h = component('/survey/availability'); h.seedOwner()
    h.state.fillDefinition.value = typesModule.exports.exampleDefinition()
    h.state.fillVersionId.value = 'availability-v1'; h.state.fillToken.value = 'test-fill'
    h.state.fillAnswers.value = { q_age: 31 }
    const calls = [], failure = new ApiError(409, code, '服务端停止收集原因')
    h.network.post = async path => { calls.push(path); throw failure }
    await h.state.submitFill()
    assert.deepEqual(calls, ['/survey/availability/evaluate'])
    assert.equal(h.state.error.value.code, code)
    assert.equal(h.state.pending.value, null)
    assert.deepEqual(plain(h.state.fillAnswers.value), { q_age: 31 })
    h.state.logout()
  })

  test(`a stale open invitation adopts ${status} after a rejected start and resets with the account`, async () => {
    const h = component('/survey/availability'); h.seedOwner()
    h.state.publicInfo.value = { status: 'open', definition: { title: '可填写时打开的邀请' } }
    assert.equal(h.state.availabilityMessage.value, '')
    h.network.post = async () => { throw new ApiError(409, code, '服务端停止收集原因') }
    await h.state.startFill()
    assert.equal(h.state.publicInfo.value.status, status)
    assert.ok(h.state.availabilityMessage.value.includes(status === 'closed' ? '已经关闭' : '截止时间'))
    assert.equal(h.state.fillDefinition.value, null)
    h.state.logout()
    assert.equal(h.state.availabilityMessage.value, '')
  })
}

test('closing requires a separate confirmation, Escape cancels, and confirm uses the displayed revision', async () => {
  const h = component(); h.seedOwner()
  h.state.active.value = { id: 'closing', status: 'open', settingsRevision: 1, draft: { title: '待关闭问卷', questions: [] } }
  const calls = []
  h.network.post = async (path, body) => { calls.push({ path, body: plain(body) }); return {} }
  h.network.request = async path => {
    assert.equal(path, '/surveys/closing')
    return { ...plain(h.state.active.value), status: 'closed', settingsRevision: 2, deadlineAt: null }
  }
  await h.state.requestCloseSurvey()
  assert.deepEqual(plain(h.state.closeConfirmation.value), { id: 'closing', title: '待关闭问卷', revision: 1 })
  assert.equal(calls.length, 0)
  h.state.escapeDialog({ key: 'Escape' }); await h.state.closeSurvey()
  assert.equal(calls.length, 0)
  await h.state.requestCloseSurvey()
  h.state.active.value.settingsRevision = 9
  await h.state.closeSurvey()
  assert.deepEqual(calls, [{ path: '/surveys/closing/close', body: { expectedSettingsRevision: 1 } }])
  assert.equal(h.state.active.value.status, 'closed')
  assert.equal(h.state.closeConfirmation.value, null)
  h.state.logout()
})

test('an old close confirmation cannot act on a different active survey or survive logout', async () => {
  const h = component(); h.seedOwner()
  h.state.active.value = { id: 'original', status: 'open', settingsRevision: 1, draft: { title: '原问卷' } }
  let calls = 0
  h.network.post = async () => { calls++; return {} }
  await h.state.requestCloseSurvey()
  h.state.active.value = { id: 'other', status: 'open', settingsRevision: 2, draft: { title: '另一问卷' } }
  await h.state.closeSurvey()
  assert.equal(calls, 0)
  await h.state.requestCloseSurvey(); h.state.logout()
  assert.equal(h.state.closeConfirmation.value, null)
})

// Exercise the real submitFill(false), including its interactive evaluation and
// creation of the recovery key. Only HTTP results are controlled by the harness.
async function submitWithUnconfirmed503(h) {
  h.api.setToken('token-A'); h.state.user.value = { id: 'A', username: 'account_A' }
  h.state.fillDefinition.value = {
    title: '恢复提交', description: '', allowAnonymous: true, entryQuestionId: 'q_age', questions: [
      { id: 'q_age', kind: 'number', title: '年龄', required: true,
        config: { min: 0, max: 120, integerOnly: true }, defaultTarget: 'q_note', rules: [] },
      { id: 'q_note', kind: 'text', title: '说明', required: false,
        config: { minLength: null, maxLength: 100 }, defaultTarget: 'END', rules: [] },
    ],
  }
  h.state.fillVersionId.value = 'version-original-A'
  h.state.fillToken.value = 'fill-original-A'
  h.state.fillAnswers.value = { q_age: 0, q_note: '  保留原始请求  ' }
  h.state.anonymous.value = true
  const calls = []
  h.network.post = async (path, body, headers) => {
    calls.push({ path, body: plain(body), headers: plain(headers || {}) })
    if (path === '/survey/public-recovery/evaluate') return {
      path: ['q_age', 'q_note'], activeAnswers: { q_age: 0, q_note: '保留原始请求' },
      trace: [], errors: [], blockedAt: null, complete: true, provisional: false,
    }
    assert.equal(path, '/survey/public-recovery/submit')
    throw new ApiError(503, 'SUBMISSION_OUTCOME_UNKNOWN', '结果暂未确认，请保留原标识重试')
  }
  await h.state.submitFill()
  assert.deepEqual(calls.map(call => call.path), [
    '/survey/public-recovery/evaluate', '/survey/public-recovery/submit',
  ])
  assert.equal(h.state.error.value?.status, 503)
  assert.equal(h.state.receipt.value, null)
  assert.ok(h.state.pending.value)
  return { calls, pending: plain(h.state.pending.value) }
}

test('401 clears account A data before account B signs in', async () => {
  const h = component(); h.seedOwner()
  h.network.request = async () => { throw new ApiError(401, 'UNAUTHENTICATED', '请先登录') }
  await h.state.perform(h.state.refreshStats)
  assert.equal(h.state.user.value, null); assert.equal(h.state.page.value, 'dashboard')
  assert.equal(h.state.active.value, null); assert.equal(h.state.stats.value, null)
  assert.equal(h.state.detailItems.value.length, 0); assert.equal(h.state.versions.value.length, 0)
  assert.equal(h.state.showHistory.value, false); assert.equal(h.state.error.value.message, '请先登录')
  h.network.request = async () => ({ items: [] })
  await h.loginB()
  assert.equal(h.state.stats.value, null); assert.equal(h.state.detailItems.value.length, 0)
  assert.equal(h.state.page.value, 'dashboard'); assert.equal(h.state.error.value, null)
})

test('late 401 from A cannot log out a newly authenticated B', async () => {
  const h = component(), oldResponse = deferred(); h.seedOwner()
  h.network.request = () => oldResponse.promise
  const oldRequest = h.state.perform(h.state.refreshStats)
  h.state.logout(); h.network.request = async () => ({ items: [] })
  await h.loginB()
  oldResponse.reject(new ApiError(401, 'EXPIRED', 'old token expired'))
  await oldRequest
  assert.equal(h.state.user.value.id, 'B'); assert.equal(h.api.getToken(), 'token-B')
  assert.equal(h.state.stats.value, null); assert.equal(h.state.error.value, null)
})

test('late successful statistics from A cannot repopulate account B view', async () => {
  const h = component(), oldResponse = deferred(); h.seedOwner()
  h.network.request = () => oldResponse.promise
  const oldRequest = h.state.perform(h.state.refreshStats)
  h.state.logout(); h.network.request = async () => ({ items: [] })
  await h.loginB()
  oldResponse.resolve({ totalResponses: 999, questions: [{ title: 'private A answer' }] })
  await oldRequest
  assert.equal(h.state.user.value.id, 'B'); assert.equal(h.state.stats.value, null)
  assert.equal(h.state.detailItems.value.length, 0); assert.equal(h.state.error.value, null)
})

test('late submission receipt stays out of B and keeps A recovery record', async () => {
  const h = component('/survey/public-test'), submission = deferred(); h.seedOwner()
  h.state.pending.value = { key: 'submission-A', body: { versionId: 'version-A', anonymous: true, answers: {} }, fillToken: 'fill-A' }
  h.state.rememberPending()
  const recoveryKey = 'wenchuan_pending_public-test_A', original = h.sessionStorage.getItem(recoveryKey)
  h.network.post = () => submission.promise
  const oldRequest = h.state.submitFill(true)
  h.state.logout()
  h.network.post = async () => ({ accessToken: 'token-B', user: { id: 'B', username: 'account_B' } })
  await h.loginB()
  submission.resolve({ receiptId: 'receipt-A', versionId: 'version-A' })
  await oldRequest
  assert.equal(h.state.user.value.id, 'B'); assert.equal(h.state.receipt.value, null)
  assert.equal(h.state.pending.value, null); assert.equal(h.sessionStorage.getItem(recoveryKey), original)
  assert.equal(h.sessionStorage.getItem('wenchuan_pending_public-test_B'), null)
})

test('submitFill retains its original request and recovery record after a simulated 503', async () => {
  const h = component('/survey/public-recovery')
  const { calls, pending } = await submitWithUnconfirmed503(h)
  const submission = calls[1]
  assert.equal(submission.headers['Idempotency-Key'], pending.key)
  assert.equal(submission.headers['X-Fill-Token'], 'fill-original-A')
  assert.equal(pending.fillToken, 'fill-original-A')
  assert.deepEqual(submission.body, {
    versionId: 'version-original-A', anonymous: true,
    answers: { q_age: 0, q_note: '  保留原始请求  ' },
  })
  assert.deepEqual(pending.body, submission.body)
  assert.deepEqual(JSON.parse(h.sessionStorage.getItem('wenchuan_pending_public-recovery_A')), pending)
  assert.equal(h.state.error.value.code, 'SUBMISSION_OUTCOME_UNKNOWN')
  assert.equal(h.state.busy.value, false)
})

test('same-account login after logout and refresh replays only the exact saved submission', async () => {
  const shared = { localStorage: storage(), sessionStorage: storage() }
  const first = component('/survey/public-recovery', shared)
  const original = await submitWithUnconfirmed503(first)
  const recoveryKey = 'wenchuan_pending_public-recovery_A'
  const stored = shared.sessionStorage.getItem(recoveryKey)
  first.state.logout()
  assert.equal(first.state.pending.value, null)
  assert.equal(shared.sessionStorage.getItem(recoveryKey), stored)

  // A fresh setup instance and the same two Storage objects model a same-tab
  // refresh. Login must select A's recovery key before any submit is attempted.
  const refreshed = component('/survey/public-recovery', shared)
  const calls = []
  refreshed.network.post = async (path, body, headers) => {
    calls.push({ path, body: plain(body), headers: plain(headers || {}) })
    if (path === '/auth/login') return {
      accessToken: 'token-A-new', user: { id: 'A', username: 'account_A' },
    }
    assert.equal(path, '/survey/public-recovery/submit', 'recovery must not start or evaluate a new attempt')
    return { receiptId: 'receipt-original-A', versionId: 'version-original-A',
      anonymous: true, acceptedAt: '2026-10-20T00:00:00Z', status: 'accepted' }
  }
  refreshed.state.username.value = 'account_A'; refreshed.state.password.value = 'test-only'
  await refreshed.state.authenticate()
  assert.equal(refreshed.state.user.value?.id, 'A')
  assert.equal(refreshed.api.getToken(), 'token-A-new')
  assert.deepEqual(plain(refreshed.state.pending.value), original.pending)
  assert.equal(refreshed.state.fillDefinition.value, null)
  assert.deepEqual(plain(refreshed.state.fillAnswers.value), {})

  await refreshed.state.submitFill(true)
  assert.deepEqual(calls.map(call => call.path), ['/auth/login', '/survey/public-recovery/submit'])
  assert.deepEqual(calls[1], original.calls[1], 'the recovery key, raw body and fill credential must all remain unchanged')
  assert.equal(refreshed.state.receipt.value?.receiptId, 'receipt-original-A')
  assert.equal(refreshed.state.pending.value, null)
  assert.equal(refreshed.state.error.value, null)
  assert.equal(shared.sessionStorage.getItem(recoveryKey), null)
})

test('token change in another page clears this view without deleting its new token', async () => {
  const h = component(); h.seedOwner()
  h.network.request = async path => path === '/auth/me' ? { id: 'A', username: 'account_A' } : { items: [] }
  await h.mount()
  h.api.setToken('token-B')
  h.events.get('storage')({ key: 'wenchuan_token', storageArea: h.localStorage })
  assert.equal(h.api.getToken(), 'token-B'); assert.equal(h.state.user.value, null)
  assert.equal(h.state.page.value, 'dashboard'); assert.equal(h.state.stats.value, null)
  assert.equal(h.state.detailItems.value.length, 0); assert.equal(h.state.showHistory.value, false)
})

test('CSV download allows current session and discards a blob after switching accounts', async () => {
  const module = { exports: {} }, blob = deferred(), blobStarted = deferred()
  let current = true, downloads = 0, revocations = 0
  const source = readFileSync(resolve(root, 'src/api.ts'), 'utf8').replace('import.meta.env.VITE_API_BASE', 'undefined')
  vm.runInNewContext(compile(source), { module, exports: module.exports, Headers,
    localStorage: { getItem: () => 'test-token' },
    fetch: async () => ({ ok: true, blob: () => { blobStarted.resolve(); return blob.promise } }),
    URL: { createObjectURL: () => 'blob:test', revokeObjectURL: () => { revocations++ } },
    document: { createElement: () => ({ click: () => { downloads++ } }) },
  })
  const waiting = module.exports.download('/export', 'test.csv', () => current)
  await blobStarted.promise; current = false; blob.resolve({}); await waiting
  assert.equal(downloads, 0); assert.equal(revocations, 0)
  current = true
  await module.exports.download('/export', 'test.csv', () => current)
  assert.equal(downloads, 1); assert.equal(revocations, 1)
})


function filteredStats(mode, count = 2) {
  return { versionId: 'private-version', versionNo: 2, respondentType: mode, asOfSeq: 7,
    totalResponses: count, uniqueParticipants: 1, anonymousResponses: mode === 'named' ? 0 : count,
    questions: [{ questionId: 'q_age', title: '年龄' }, { questionId: 'q_note', title: '备注' }] }
}

test('respondent filter preserves the boundary for details, pagination and CSV', async () => {
  const h = component(); h.seedOwner()
  h.state.stats.value = filteredStats('all', 4)
  h.state.versions.value = [{ id: 'private-version', versionNo: 2, definition: { title: '历史标题' } }]
  h.state.statsRespondentType.value = 'anonymous'
  const calls = []
  h.network.request = async path => {
    const url = new URL(path, 'http://test'); calls.push(url)
    if (url.pathname.endsWith('/stats')) return filteredStats('anonymous')
    return { items: [{ receiptId: 'anonymous-6', anonymous: true }], nextCursor: 5 }
  }
  await h.state.refreshStats(false)
  assert.equal(calls[0].searchParams.get('asOfSeq'), '7')
  h.state.statsQuestion.value = 'q_age'
  await h.state.loadDetails(true)
  await h.state.loadDetails()
  assert.equal(calls[3].searchParams.get('cursor'), '5')
  assert.equal(calls[3].searchParams.get('questionId'), 'q_age')
  for (const url of calls) {
    assert.equal(url.searchParams.get('respondentType'), 'anonymous')
    assert.equal(url.searchParams.get('versionId'), 'private-version')
    assert.equal(url.searchParams.get('asOfSeq'), '7')
  }
  let saved
  h.api.download = async (path, filename, isCurrent) => { saved = { path, filename, isCurrent } }
  await h.state.exportCSV()
  const exported = new URL(saved.path, 'http://test')
  assert.equal(exported.searchParams.get('respondentType'), 'anonymous')
  assert.equal(exported.searchParams.get('asOfSeq'), '7')
  assert.equal(exported.searchParams.has('questionId'), false, 'CSV includes every question')
  assert.match(saved.filename, /历史标题-V2-匿名-范围7\.csv$/)
  assert.equal(saved.isCurrent(), true)
  h.state.statsRespondentType.value = 'named'
  assert.equal(saved.isCurrent(), false, 'an old filtered download must not complete into a new selection')
})

test('late statistics cannot replace a newer respondent filter', async () => {
  const h = component(); h.seedOwner()
  h.state.stats.value = filteredStats('all', 4)
  h.state.versions.value = [{ id: 'private-version', versionNo: 1, definition: plain(typesModule.exports.exampleDefinition()) }]
  const old = deferred(), calls = []
  h.network.request = async path => {
    const url = new URL(path, 'http://test'); calls.push(url)
    if (url.pathname.endsWith('/responses')) return { items: [{ receiptId: 'named-result' }], nextCursor: null }
    return url.searchParams.get('respondentType') === 'anonymous' ? old.promise : filteredStats('named', 3)
  }
  h.state.statsRespondentType.value = 'anonymous'
  const first = h.state.refreshStats(false)
  h.state.statsRespondentType.value = 'named'
  await h.state.refreshStats(false)
  old.resolve(filteredStats('anonymous', 99)); await first
  assert.equal(h.state.stats.value.respondentType, 'named')
  assert.equal(h.state.stats.value.totalResponses, 3)
  assert.deepEqual(plain(h.state.detailItems.value), [{ receiptId: 'named-result' }])
  assert.equal(calls[1].searchParams.get('asOfSeq'), '7', 'the last confirmed boundary survives an in-flight switch')
  assert.equal(calls.filter(url => url.pathname.endsWith('/responses')).length, 1)
  h.state.logout()
  assert.equal(h.state.statsRespondentType.value, 'all')
})

test('late question details cannot replace a newer selection in the same filtered view', async () => {
  const h = component(); h.seedOwner()
  h.state.stats.value = filteredStats('named')
  h.state.statsRespondentType.value = 'named'
  const old = deferred()
  h.network.request = path => new URL(path, 'http://test').searchParams.get('questionId') === 'q_age'
    ? old.promise : Promise.resolve({ items: [{ receiptId: 'note-result' }], nextCursor: null })
  h.state.statsQuestion.value = 'q_age'
  const first = h.state.loadDetails(true)
  h.state.statsQuestion.value = 'q_note'
  await h.state.loadDetails(true)
  old.resolve({ items: [{ receiptId: 'old-age-result' }], nextCursor: 3 }); await first
  assert.deepEqual(plain(h.state.detailItems.value), [{ receiptId: 'note-result' }])
  assert.equal(h.state.nextDetailCursor.value, null)
})


test('opening another survey statistics clears the previous question and respondent filter', async () => {
  const h = component(); h.seedOwner()
  h.state.statsQuestion.value = 'question-from-A'; h.state.statsRespondentType.value = 'anonymous'
  const calls = []
  h.network.request = async path => {
    const url = new URL(path, 'http://test'); calls.push(url)
    if (url.pathname === '/surveys/survey-B') return { id: 'survey-B', currentVersionId: 'version-B',
      draft: { title: '问卷B', questions: [] }, deadlineAt: null, allowMultiple: true }
    if (url.pathname.endsWith('/versions')) return { items: [{ id: 'version-B', definition: { title: '问卷B' } }] }
    if (url.pathname.endsWith('/stats')) return { ...filteredStats('all'), versionId: 'version-B' }
    assert.ok(url.pathname.endsWith('/responses'))
    return { items: [{ receiptId: 'B-result' }], nextCursor: null }
  }
  await h.state.openSurvey('survey-B', 'statistics')
  assert.equal(h.state.error.value, null)
  assert.equal(h.state.statsQuestion.value, ''); assert.equal(h.state.statsRespondentType.value, 'all')
  const detail = calls.find(url => url.pathname.endsWith('/responses'))
  assert.equal(detail.searchParams.has('questionId'), false)
  assert.equal(detail.searchParams.get('versionId'), 'version-B')
  assert.equal(detail.searchParams.get('respondentType'), 'all')
  assert.deepEqual(plain(h.state.detailItems.value), [{ receiptId: 'B-result' }])
})


function definitionFailure() {
  const failure = new ApiError(422, 'INVALID_DEFINITION', '发布检查未通过')
  failure.fieldErrors = [{ questionId: 'q_age', ruleId: 'r_minor',
    field: 'questions[0].rules[0].condition.value', code: 'invalid_number', message: '比较值必须是有限安全数字' }]
  return failure
}

test('definition errors mount the editor before focusing and expire on the next edit', async () => {
  const h = component(); h.seedOwner(); h.state.page.value = 'editor'
  h.state.definition.value = typesModule.exports.exampleDefinition()
  h.network.post = async () => { throw definitionFailure() }
  await h.state.runPreview()
  assert.equal(h.state.editorFieldErrors.value.length, 1)
  const issue = h.state.editorFieldErrors.value[0], focused = []
  h.state.definitionEditor.value = { focusError: async value => {
    assert.equal(h.state.editorTab.value, 'design'); focused.push(plain(value)); return true
  } }
  await h.state.focusEditorIssue(issue)
  assert.equal(focused.length, 1)
  h.state.definition.value.questions[0].title = '修改后的题目'
  assert.equal(h.state.error.value, null, 'previous diagnostics clear synchronously with the edit')
  assert.equal(h.state.editorFieldErrors.value.length, 0)
  await h.state.focusEditorIssue(issue)
  assert.equal(focused.length, 1, 'an old error must not focus a changed definition')
  h.state.logout()
})

test('a preview response arriving during the edit debounce cannot restore an obsolete definition error', async () => {
  const h = component(), pendingPreview = deferred(); h.seedOwner(); h.state.page.value = 'editor'
  h.state.definition.value = typesModule.exports.exampleDefinition()
  h.network.post = () => pendingPreview.promise
  const old = h.state.runPreview()
  h.state.definition.value.questions.reverse()
  pendingPreview.reject(definitionFailure()); await old
  assert.equal(h.state.error.value, null)
  assert.equal(h.state.editorFieldErrors.value.length, 0)
  assert.equal(h.state.previewResult.value, null)
  h.state.logout()
})

const designWarning = { code: 'overlapping_rules', questionId: 'q_age', ruleId: 'r_minor',
  field: 'questions[0].rules[0].condition', relatedRuleIds: ['r_earlier'], message: '规则匹配范围部分重叠' }
const checkedPreview = (warnings = []) => ({ path: ['q_age'], activeAnswers: {}, trace: [],
  errors: [], blockedAt: 'q_age', complete: false, provisional: false, warnings })

test('design warnings focus the editor without becoming blocking errors and expire on edit', async () => {
  const h = component(); h.seedOwner(); h.state.page.value = 'editor'
  h.state.definition.value = typesModule.exports.exampleDefinition()
  assert.equal(h.state.currentDesignWarnings.value, null, 'unchecked is distinct from a successful empty check')
  h.network.post = async () => checkedPreview([designWarning])
  await h.state.runPreview()
  assert.equal(h.state.error.value, null)
  assert.equal(h.state.editorFieldErrors.value.length, 0)
  assert.equal(h.state.currentDesignWarnings.value.length, 1)
  const issue = h.state.currentDesignWarnings.value[0], focused = []
  h.state.definitionEditor.value = { focusError: async value => {
    assert.equal(h.state.editorTab.value, 'design'); focused.push(plain(value)); return true
  } }
  h.state.editorTab.value = 'preview'
  await h.state.focusDesignWarning(issue)
  assert.deepEqual(focused, [designWarning])
  h.state.definition.value.questions[0].rules[0].priority = 20
  assert.equal(h.state.currentDesignWarnings.value, null)
  await h.state.focusDesignWarning(issue)
  assert.equal(focused.length, 1)
  h.network.post = async () => checkedPreview()
  await h.state.runPreview()
  assert.deepEqual(plain(h.state.currentDesignWarnings.value), [])
  h.state.logout()
})

test('late design warnings cannot restore results after editing, navigation, or account changes', async () => {
  const h = component(); h.seedOwner(); h.state.page.value = 'editor'
  h.state.definition.value = typesModule.exports.exampleDefinition()
  const afterEdit = deferred(); h.network.post = () => afterEdit.promise
  const edited = h.state.runPreview()
  h.state.definition.value.questions[0].title = '新标题'
  afterEdit.resolve(checkedPreview([designWarning])); await edited
  assert.equal(h.state.currentDesignWarnings.value, null)
  assert.equal(h.state.previewResult.value, null)
  const afterNavigation = deferred(); h.network.post = () => afterNavigation.promise
  const navigated = h.state.runPreview(); h.state.page.value = 'statistics'
  afterNavigation.resolve(checkedPreview([designWarning])); await navigated
  assert.equal(h.state.currentDesignWarnings.value, null)
  h.state.page.value = 'editor'
  const afterLogin = deferred(); h.network.post = () => afterLogin.promise
  const loggedOut = h.state.runPreview(); h.state.logout()
  h.network.post = async () => ({ accessToken: 'token-B', user: { id: 'B', username: 'account_B' } })
  await h.loginB()
  afterLogin.resolve(checkedPreview([designWarning])); await loggedOut
  assert.equal(h.state.user.value.id, 'B')
  assert.equal(h.state.currentDesignWarnings.value, null)
  assert.equal(h.state.previewResult.value, null)
  h.state.logout()
})

test('the latest design check wins and failed checks cannot display a clean result', async () => {
  const h = component(); h.seedOwner(); h.state.page.value = 'editor'
  h.state.definition.value = typesModule.exports.exampleDefinition()
  const older = deferred(); h.network.post = () => older.promise
  const first = h.state.runPreview()
  h.network.post = async () => checkedPreview([designWarning])
  await h.state.runPreview()
  older.resolve(checkedPreview()); await first
  assert.equal(h.state.currentDesignWarnings.value.length, 1)
  h.network.post = async () => { throw definitionFailure() }
  await h.state.runPreview()
  assert.equal(h.state.currentDesignWarnings.value, null)
  assert.equal(h.state.editorFieldErrors.value.length, 1)
  h.network.post = async () => checkedPreview()
  await h.state.runPreview()
  assert.equal(h.state.error.value, null)
  assert.equal(h.state.editorFieldErrors.value.length, 0)
  assert.deepEqual(plain(h.state.currentDesignWarnings.value), [])
  h.network.post = async () => { const value = checkedPreview(); delete value.warnings; return value }
  await h.state.runPreview()
  assert.equal(h.state.currentDesignWarnings.value, null, 'a response without an analysis result is not a clean check')
  h.state.logout()
})


test('bank pending work protects every workspace exit and page unload', async () => {
  const h = component(); h.seedOwner(); h.state.page.value = 'banks'
  h.state.questionBanks.value = { canLeave: () => false, hasUnsavedChanges: () => true }
  h.network.request = async () => { throw new Error('A blocked bank exit must not read') }
  h.network.post = async () => { throw new Error('A blocked bank exit must not write') }
  await h.state.goDashboard(); await h.state.openSurvey('another'); await h.state.createSurvey()
  h.state.openQuestions()
  assert.equal(h.state.page.value, 'banks')
  let prevented = false; h.state.beforeUnload({ preventDefault: () => { prevented = true } })
  assert.equal(prevented, true)
  h.state.questionBanks.value = null; h.state.logout()
})

test('opening banks checks question work then clears the old survey editing context', () => {
  const h = component(); seedEditor(h, false); h.state.page.value = 'questions'
  h.state.questionLibrary.value = { canLeave: () => false, hasUnsavedChanges: () => true }
  h.state.openBanks(); assert.equal(h.state.page.value, 'questions')
  h.state.questionLibrary.value = { canLeave: () => true, hasUnsavedChanges: () => false }
  h.state.openBanks()
  assert.equal(h.state.page.value, 'banks'); assert.equal(h.state.active.value, null)
  assert.equal(h.state.definition.value, null); assert.equal(h.state.questionPicker.value, null)
  h.state.logout()
})

test('bank navigation preserves an unsaved survey when leaving is declined', () => {
  const h = component('/', { confirm: () => false }); seedEditor(h, false)
  h.state.definition.value.description = '尚未保存的修改'
  const before = JSON.stringify(h.state.definition.value)
  h.state.openBanks()
  assert.equal(h.state.page.value, 'editor'); assert.equal(JSON.stringify(h.state.definition.value), before)
  h.state.logout()
})

test('picker source switches, close and escape respect active source work and busy state', () => {
  const h = component(); seedEditor(h, false); h.state.openQuestionPicker()
  h.state.pickerLibrary.value = { canLeave: () => false, hasUnsavedChanges: () => true }
  h.state.changePickerCollection('banks'); h.state.closeQuestionPicker()
  assert.equal(h.state.pickerCollection.value, 'questions'); assert.ok(h.state.questionPicker.value)
  h.state.pickerLibrary.value = null; h.state.changePickerCollection('banks')
  assert.equal(h.state.pickerCollection.value, 'banks')
  h.state.questionBusy.value = true
  h.state.changePickerCollection('questions'); h.state.escapeDialog({ key: 'Escape' })
  assert.equal(h.state.pickerCollection.value, 'banks'); assert.ok(h.state.questionPicker.value)
  h.state.questionBusy.value = false
  h.state.pickerBanks.value = { canLeave: () => false, hasUnsavedChanges: () => true }
  h.state.escapeDialog({ key: 'Escape' }); assert.ok(h.state.questionPicker.value)
  h.state.pickerBanks.value = null; h.state.closeQuestionPicker()
  assert.equal(h.state.questionPicker.value, null); h.state.logout()
})

test('bank expiry clears busy refs and picker source even when manual departure is blocked', () => {
  const h = component('/', { confirm: () => false }); h.seedOwner(); h.state.page.value = 'banks'
  h.state.questionBanks.value = { canLeave: () => false, hasUnsavedChanges: () => true }
  h.state.pickerCollection.value = 'banks'; h.state.questionBusy.value = true
  h.state.logout(); assert.equal(h.api.getToken(), 'token-A')
  h.state.expireQuestionSession()
  assert.equal(h.api.getToken(), ''); assert.equal(h.state.page.value, 'dashboard')
  assert.equal(h.state.questionBanks.value, null); assert.equal(h.state.questionBusy.value, false)
  assert.equal(h.state.pickerCollection.value, 'questions'); assert.equal(h.state.error.value.status, 401)
})


test('pending cross-survey statistics protects navigation, logout and page unload', async () => {
  const h = component('/', { confirm: () => false }); seedEditor(h, false)
  const before = JSON.stringify(h.state.definition.value), surveyId = h.state.active.value.id
  h.state.openQuestionStatistics('fixed-question')
  h.state.questionStatistics.value = { canLeave: () => false, hasUnsavedChanges: () => true }
  // A retained editor settings ref must not be selected instead of this page.
  h.state.statisticsGrants.value = { canLeave: () => true, hasUnsavedChanges: () => false }
  assert.equal(h.state.questionWork(), h.state.questionStatistics.value)
  h.network.request = async () => { throw new Error('Blocked navigation must not read') }
  await h.state.goDashboard(); h.state.openQuestions(); await h.state.showStats()
  h.state.returnToEditor(); h.state.changeEditorTab('settings')
  assert.equal(h.state.page.value, 'question-statistics')
  assert.equal(h.state.editorTab.value, 'design')
  assert.equal(h.state.statQuestionId.value, 'fixed-question')
  assert.equal(h.state.active.value.id, surveyId)
  assert.equal(JSON.stringify(h.state.definition.value), before)
  const unload = { preventDefault() { this.prevented = true } }
  h.state.beforeUnload(unload)
  assert.equal(unload.prevented, true); assert.equal(unload.returnValue, '')
  h.state.logout(); assert.equal(h.api.getToken(), 'token-A')
  h.state.expireQuestionSession()
})

test('unconfirmed statistics grants block settings tab exits and workspace navigation', async () => {
  const h = component('/', { confirm: () => false }); seedEditor(h, false)
  h.state.editorTab.value = 'settings'
  h.state.statisticsGrants.value = { canLeave: () => false, hasUnsavedChanges: () => true }
  h.state.questionStatistics.value = { canLeave: () => true, hasUnsavedChanges: () => false }
  assert.equal(h.state.questionWork(), h.state.statisticsGrants.value)
  h.network.request = async () => { throw new Error('Unconfirmed grant must not trigger a workspace read') }
  h.state.changeEditorTab('design'); h.state.openQuestions(); h.state.openQuestionStatistics('another-question')
  await h.state.showStats(); await h.state.goDashboard()
  assert.equal(h.state.page.value, 'editor'); assert.equal(h.state.editorTab.value, 'settings')
  assert.equal(h.state.statQuestionId.value, '')
  let prevented = false; h.state.beforeUnload({ preventDefault: () => { prevented = true } })
  assert.equal(prevented, true)
  h.state.logout(); assert.equal(h.api.getToken(), 'token-A')
  h.state.expireQuestionSession()
})

test('the question statistics detour retains unsaved draft and fixed sources when returning', () => {
  const h = component(); seedEditor(h, true)
  const original = JSON.stringify(h.state.definition.value)
  const mappings = [{ instanceId: 'q_age', questionId: 'bank-question', questionVersionId: 'bank-v1' }]
  h.state.localQuestionSources.value = mappings
  h.state.openQuestionStatistics('bank-question')
  assert.equal(h.state.page.value, 'question-statistics')
  assert.equal(h.state.statQuestionId.value, 'bank-question')
  h.state.questionStatistics.value = { canLeave: () => false, hasUnsavedChanges: () => true }
  h.state.returnToEditor(); assert.equal(h.state.page.value, 'question-statistics')
  h.state.questionStatistics.value = { canLeave: () => true, hasUnsavedChanges: () => false }
  h.state.returnToEditor()
  assert.equal(h.state.page.value, 'editor'); assert.equal(h.state.dirty.value, true)
  assert.equal(JSON.stringify(h.state.definition.value), original)
  assert.deepEqual(plain(h.state.currentQuestionSources.value), mappings)
  h.state.expireQuestionSession()
})

test('401 forcibly clears both statistics refs and selected question before another account logs in', async () => {
  const h = component('/', { confirm: () => false }); seedEditor(h, false)
  h.state.page.value = 'question-statistics'; h.state.statQuestionId.value = 'private-question'
  h.state.questionStatistics.value = { canLeave: () => false, hasUnsavedChanges: () => true }
  h.state.statisticsGrants.value = { canLeave: () => false, hasUnsavedChanges: () => true }
  h.state.questionBusy.value = true
  h.state.logout(); assert.equal(h.api.getToken(), 'token-A')
  await h.state.perform(async () => { throw new ApiError(401, 'UNAUTHENTICATED', 'expired') })
  assert.equal(h.api.getToken(), ''); assert.equal(h.state.page.value, 'dashboard')
  assert.equal(h.state.statQuestionId.value, '')
  assert.equal(h.state.questionStatistics.value, null); assert.equal(h.state.statisticsGrants.value, null)
  assert.equal(h.state.questionBusy.value, false); assert.equal(h.state.active.value, null)
  assert.equal(h.state.definition.value, null); assert.equal(h.state.error.value.status, 401)
  await h.loginB()
  assert.equal(h.state.statQuestionId.value, ''); assert.equal(h.state.questionWork(), null)
  assert.equal(h.state.questionStatistics.value, null); assert.equal(h.state.statisticsGrants.value, null)
  h.state.logout()
})


test('a fixed-source statistics entry resolves the current question mapping without changing drafts or granting data access', () => {
  const h = component(); seedEditor(h, true)
  const instanceId = h.state.definition.value.questions[0].id
  const localId = h.state.definition.value.questions[1].id
  h.state.active.value.draftQuestionSources = [{ instanceId, questionId: 'old-source-question', questionVersionId: 'old-v1' }]
  const sources = [{ instanceId, questionId: 'current-fixed-question', questionVersionId: 'current-v2' },
    { instanceId: 'deleted-instance', questionId: 'stale-question', questionVersionId: 'stale-v1' }]
  h.state.localQuestionSources.value = sources
  const before = JSON.stringify({ draft: h.state.definition.value, saved: h.state.savedDefinition.value,
    active: h.state.active.value, sources: h.state.currentQuestionSources.value })
  const requests = []
  h.network.request = async (...args) => { requests.push(args); throw new Error('Opening statistics must not read or grant') }
  h.network.post = async (...args) => { requests.push(args); throw new Error('Opening statistics must not read or grant') }
  h.state.openSourceQuestionStatistics(localId)
  h.state.openSourceQuestionStatistics('deleted-instance')
  h.state.openSourceQuestionStatistics('missing-instance')
  assert.equal(h.state.page.value, 'editor'); assert.equal(h.state.statQuestionId.value, '')
  h.state.page.value = 'questions'; h.state.openSourceQuestionStatistics(instanceId)
  assert.equal(h.state.page.value, 'questions'); assert.equal(h.state.statQuestionId.value, '')
  h.state.page.value = 'editor'; h.state.editorTab.value = 'settings'; h.state.openSourceQuestionStatistics(instanceId)
  assert.equal(h.state.page.value, 'editor'); assert.equal(h.state.statQuestionId.value, '')
  h.state.editorTab.value = 'design'; h.state.questionBusy.value = true; h.state.openSourceQuestionStatistics(instanceId)
  assert.equal(h.state.page.value, 'editor'); assert.equal(h.state.statQuestionId.value, '')
  h.state.questionBusy.value = false; h.state.openSourceQuestionStatistics(instanceId)
  assert.equal(h.state.page.value, 'question-statistics')
  assert.equal(h.state.statQuestionId.value, 'current-fixed-question', 'use the current source question ID, never the local instance or saved stale mapping')
  h.state.returnToEditor()
  assert.equal(h.state.page.value, 'editor'); assert.equal(h.state.dirty.value, true)
  assert.equal(JSON.stringify({ draft: h.state.definition.value, saved: h.state.savedDefinition.value,
    active: h.state.active.value, sources: h.state.currentQuestionSources.value }), before)
  assert.equal(requests.length, 0, 'entry navigation must not silently grant access or create a range')
  h.state.expireQuestionSession()
})


function historySummary(number) {
  return { id: `version-${number}`, surveyId: 'private-survey', versionNo: number,
    title: `历史标题${number}`, sourceDraftRevision: number, publishedAt: '2026-10-29T00:00:00Z' }
}
function historyDefinition(number) {
  return { ...historySummary(number), definition: { ...plain(typesModule.exports.exampleDefinition()), title: `历史标题${number}` } }
}

test('opening a 201-version survey reads one summary page and no complete history', async () => {
  const h = component(), original = seedEditor(h), calls = []
  h.network.request = async path => {
    calls.push(path)
    if (path === '/surveys/private-survey') return { ...original, lastVersionNo: 201, currentVersionId: 'version-201' }
    const url = new URL(path, 'http://test')
    assert.equal(url.searchParams.get('summary'), 'true'); assert.equal(url.searchParams.get('offset'), '0')
    return { items: Array.from({ length: 100 }, (_, i) => historySummary(201 - i)) }
  }
  await h.state.openSurvey('private-survey')
  assert.equal(h.state.error.value, null); assert.equal(calls.length, 2)
  assert.equal(h.state.versions.value.length, 100); assert.equal(h.state.nextVersionOffset.value, 100)
  assert.deepEqual(plain(h.state.versionDetails.value), {})
  assert.equal(h.state.versions.value.some(v => 'definition' in v), false)
  h.state.logout()
})

test('older history summaries load only on request and preserve the earlier page on failure', async () => {
  const h = component(); seedEditor(h)
  h.state.versions.value = Array.from({ length: 100 }, (_, i) => historySummary(201 - i))
  h.state.nextVersionOffset.value = 100
  const urls = []; let fail = true
  h.network.request = async path => {
    const url = new URL(path, 'http://test'); urls.push(url)
    assert.equal(url.searchParams.get('summary'), 'true')
    if (fail) throw new ApiError(503, 'TEMPORARY')
    const offset = Number(url.searchParams.get('offset'))
    return { items: offset === 100 ? Array.from({ length: 100 }, (_, i) => historySummary(101 - i)) : [historySummary(1)] }
  }
  await h.state.loadMoreVersions(); assert.equal(h.state.versions.value.length, 100); assert.equal(h.state.nextVersionOffset.value, 100)
  fail = false; await h.state.loadMoreVersions(); assert.equal(h.state.versions.value.length, 200); assert.equal(h.state.nextVersionOffset.value, 200)
  await h.state.loadMoreVersions(); assert.equal(h.state.versions.value.length, 201); assert.equal(h.state.nextVersionOffset.value, null)
  assert.deepEqual(urls.map(url => url.searchParams.get('offset')), ['100', '100', '200'])
  assert.equal(new Set(h.state.versions.value.map(v => v.id)).size, 201)
  h.state.logout()
})

test('history comparison reads only the two selected immutable definitions and reuses them', async () => {
  const h = component(); seedEditor(h)
  const calls = []
  h.network.request = async path => {
    calls.push(path)
    if (path.includes('/versions?')) return { items: [historySummary(3), historySummary(2), historySummary(1)] }
    return historyDefinition(Number(path.split('-').at(-1)))
  }
  await h.state.openHistory()
  assert.equal(h.state.error.value, null); assert.equal(h.state.showHistory.value, true)
  assert.equal(h.state.historyComparisonReady.value, true)
  assert.deepEqual(calls.filter(path => !path.includes('?')), ['/surveys/private-survey/versions/version-2', '/surveys/private-survey/versions/version-3'])
  await h.state.compareHistory(); assert.equal(calls.length, 3)
  h.state.leftVersionId.value = 'version-1'; await h.state.compareHistory()
  assert.equal(calls.at(-1), '/surveys/private-survey/versions/version-1'); assert.equal(calls.length, 4)
  assert.ok(h.state.differences.value.some(row => row.title === '问卷标题'))
  h.state.logout()
})

test('a failed history detail does not claim identical definitions and can retry', async () => {
  const h = component(); seedEditor(h)
  let fail = true
  h.network.request = async path => {
    if (path.includes('/versions?')) return { items: [historySummary(2), historySummary(1)] }
    if (path.endsWith('version-1') && fail) throw new ApiError(503, 'TEMPORARY')
    return historyDefinition(Number(path.split('-').at(-1)))
  }
  await h.state.openHistory()
  assert.equal(h.state.showHistory.value, true); assert.equal(h.state.historyComparisonReady.value, false)
  assert.equal(h.state.error.value.code, 'TEMPORARY')
  fail = false; await h.state.compareHistory()
  assert.equal(h.state.error.value, null); assert.equal(h.state.historyComparisonReady.value, true)
  h.state.logout()
})

test('copying an unloaded history summary first reads its complete definition and fixed sources', async () => {
  const h = component(), original = seedEditor(h), calls = []
  const complete = { ...historyDefinition(1), questionSources: [{ instanceId: 'q_age', questionId: 'bank-age', questionVersionId: 'bank-age-v1' }] }
  h.state.versions.value = [historySummary(1)]
  h.network.request = async (path, options) => {
    calls.push(path)
    if (path.endsWith('/versions/version-1')) return complete
    if (path.endsWith('/draft')) {
      assert.deepEqual(JSON.parse(options.body), { definition: complete.definition, expectedDraftRevision: 4, questionSources: complete.questionSources })
      return {}
    }
    if (path.includes('/versions?')) return { items: [historySummary(1)] }
    return { ...original, draft: complete.definition, draftQuestionSources: complete.questionSources, draftRevision: 5 }
  }
  await h.state.copyVersion(historySummary(1))
  assert.equal(h.state.error.value, null); assert.equal(calls[0], '/surveys/private-survey/versions/version-1')
  assert.equal(h.state.definition.value.title, complete.definition.title)
  assert.deepEqual(plain(h.state.currentQuestionSources.value), complete.questionSources)
  h.state.logout()
})

test('an unloaded history detail cannot trigger a draft write after switching accounts', async () => {
  const h = component(); seedEditor(h)
  const wait = deferred(), calls = []
  h.network.request = async (path, options) => { calls.push({ path, options }); return path.startsWith('/surveys?') ? { items: [] } : wait.promise }
  const action = h.state.copyVersion(historySummary(1))
  h.state.logout(); await h.loginB(); wait.resolve(historyDefinition(1)); await action
  assert.equal(h.state.user.value.id, 'B'); assert.deepEqual(plain(h.state.versionDetails.value), {})
  assert.equal(calls.some(call => call.options?.method === 'PUT'), false)
  assert.equal(h.state.active.value, null); assert.equal(h.state.busy.value, false)
  h.state.logout()
})

test('single-survey statistics loads only its selected full version to interpret answer labels', async () => {
  const h = component(); seedEditor(h)
  h.state.versions.value = [historySummary(2), historySummary(1)]
  h.state.statsVersion.value = 'version-1'
  const calls = [], complete = historyDefinition(1)
  h.network.request = async path => {
    calls.push(path)
    if (path.endsWith('/versions/version-1')) return complete
    if (path.includes('/stats?')) return { ...filteredStats('all'), versionId: 'version-1' }
    return { items: [], nextCursor: null }
  }
  await h.state.refreshStats()
  assert.equal(calls[0], '/surveys/private-survey/versions/version-1')
  assert.equal(calls.length, 3); assert.equal(h.state.stats.value.versionId, 'version-1')
  assert.deepEqual(Object.keys(h.state.versionDetails.value), ['version-1'])
  h.state.logout()
})
