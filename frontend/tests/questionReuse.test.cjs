const assert = require('node:assert/strict')
const { readFileSync } = require('node:fs')
const { resolve } = require('node:path')
const { webcrypto } = require('node:crypto')
const { test } = require('node:test')
const vm = require('node:vm')
const ts = require('typescript')
const compile = source => ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText
const types = { exports: {} }
vm.runInNewContext(compile(readFileSync(resolve(__dirname, '../src/types.ts'), 'utf8')), { module: types, exports: types.exports, crypto: webcrypto })
const reuse = { exports: {} }
vm.runInNewContext(compile(readFileSync(resolve(__dirname, '../src/questionReuse.ts'), 'utf8')), { module: reuse, exports: reuse.exports, require: name => { assert.equal(name, './types'); return types.exports } })
const { applyQuestionVersion, detachQuestionSource } = reuse.exports
const plain = value => JSON.parse(JSON.stringify(value))
const version = (id='bank-v1') => ({ id, questionId: 'bank-question', versionNo: 1, content: { kind: 'number', title: '评分', config: { min: 0, max: 10, integerOnly: true } } })
const empty = () => ({ title: '调查', description: '', allowAnonymous: true, entryQuestionId: null, questions: [] })

test('adding a saved version gives a local instance, entry and fixed source without changing inputs', () => {
  const original = empty(), saved = version(), result = applyQuestionVersion(original, [], saved)
  assert.deepEqual(original, empty())
  assert.equal(result.definition.entryQuestionId, result.instanceId)
  assert.notEqual(result.instanceId, saved.id)
  assert.deepEqual(plain(result.sources), [{ instanceId: result.instanceId, questionId: saved.questionId, questionVersionId: saved.id }])
  const question = result.definition.questions[0]
  assert.equal(question.required, true); assert.equal(question.defaultTarget, 'END'); assert.deepEqual(plain(question.rules), [])
  question.config.min = 3
  assert.equal(saved.content.config.min, 0)
})

test('using the same version twice produces independent instances and connects the default path', () => {
  const saved = version(), first = applyQuestionVersion(empty(), [], saved)
  const second = applyQuestionVersion(first.definition, first.sources, saved)
  assert.notEqual(first.instanceId, second.instanceId)
  assert.equal(first.definition.questions[0].defaultTarget, 'END')
  assert.equal(second.definition.questions[0].defaultTarget, second.instanceId)
  assert.equal(second.sources.length, 2)
  second.definition.questions[1].config.min = 2
  assert.equal(second.definition.questions[0].config.min, 0)
})

test('switching content preserves the local ID, requiredness and rules for backend compatibility checks', () => {
  const first = applyQuestionVersion(empty(), [], version())
  first.definition.questions[0].required = false
  first.definition.questions[0].rules = [{ id: 'r_keep', priority: 1, condition: { operator: 'lt', value: 2 }, target: 'END' }]
  const before = JSON.stringify(first)
  const next = { ...version('bank-v2'), content: { kind: 'single', title: '新选项题', config: { options: [{ id: 'yes', label: '是' }] } } }
  const result = applyQuestionVersion(first.definition, first.sources, next, first.instanceId)
  assert.equal(JSON.stringify(first), before)
  assert.equal(result.definition.questions[0].id, first.instanceId)
  assert.equal(result.definition.questions[0].required, false)
  assert.deepEqual(plain(result.definition.questions[0].rules), plain(first.definition.questions[0].rules))
  assert.equal(result.sources[0].questionVersionId, 'bank-v2')
})

test('a missing instance or a different question series cannot be used as a version switch', () => {
  const first = applyQuestionVersion(empty(), [], version()), before = JSON.stringify(first)
  assert.throws(() => applyQuestionVersion(first.definition, first.sources, version(), 'missing'))
  assert.throws(() => applyQuestionVersion(first.definition, first.sources, { ...version(), questionId: 'another' }, first.instanceId))
  assert.equal(JSON.stringify(first), before)
})

test('detaching removes provenance while retaining content and the remaining source mapping', () => {
  const first = applyQuestionVersion(empty(), [], version()), second = applyQuestionVersion(first.definition, first.sources, version())
  const before = JSON.stringify(second.definition)
  const result = detachQuestionSource(second.definition, [...second.sources, { instanceId: 'deleted', questionId: 'unused', questionVersionId: 'unused' }], first.instanceId)
  assert.equal(JSON.stringify(second.definition), before)
  assert.deepEqual(plain(result), [plain(second.sources[1])])
})

test('the one hundred question limit rejects insertion without changing the questionnaire', () => {
  const def = empty()
  def.questions = Array.from({ length: 100 }, (_, index) => ({ id: `q_${index}`, ...version().content, required: false, defaultTarget: 'END', rules: [] }))
  const before = JSON.stringify(def)
  assert.throws(() => applyQuestionVersion(def, [], version()), /100/)
  assert.equal(JSON.stringify(def), before)
})
