const assert = require('node:assert/strict')
const { readFileSync } = require('node:fs')
const { resolve } = require('node:path')
const { test } = require('node:test')
const vm = require('node:vm')
const { parse, compileScript } = require('@vue/compiler-sfc')
const ts = require('typescript')
const { reactive } = require('vue')

// Compile and execute the actual SFC setup, including its computed state and
// emitted update. The tests do not copy the mode conversion implementation.
const path = resolve(__dirname, '../src/components/ConditionEditor.vue')
const { descriptor } = parse(readFileSync(path, 'utf8'), { filename: path })
const compiled = ts.transpileModule(compileScript(descriptor, { id: 'condition-regression' }).content, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
}).outputText
const compiledModule = { exports: {} }
vm.runInNewContext(compiled, { module: compiledModule, exports: compiledModule.exports, require })
const Component = compiledModule.exports.default
const plain = value => JSON.parse(JSON.stringify(value))

function editor(condition, kind = 'number') {
  const props = reactive({ modelValue: plain(condition), question: { kind, config: {
    options: [{ id: 'read', label: '阅读' }, { id: 'sport', label: '运动' }],
  } } })
  const original = props.modelValue
  let emitted
  const setup = Component.setup(props, {
    expose() {},
    emit(event, value) { assert.equal(event, 'update:modelValue'); emitted = value },
  })
  return {
    switch(mode) { setup.changeMode(mode); props.modelValue = emitted; return plain(emitted) },
    get original() { return plain(original) },
    get value() { return plain(props.modelValue) },
  }
}

test('wrapping a configured numeric answer retains its operator and value', () => {
  const leaf = { operator: 'gte', value: 18 }
  const input = editor(leaf)
  assert.deepEqual(input.switch('allOf'), { allOf: [leaf] })
  assert.deepEqual(input.original, leaf)
})

test('AND to OR and back preserves siblings, nested groups, boundaries and ordering', () => {
  const children = [{ operator: 'gte', value: 18 },
    { anyOf: [{ operator: 'lte', value: 60 }, { operator: 'eq', value: 80 }] },
    { operator: 'between', value: { min: 0, max: 100 } }]
  const input = editor({ allOf: children })
  assert.deepEqual(input.switch('anyOf'), { anyOf: children })
  assert.deepEqual(input.switch('allOf'), { allOf: children })
  assert.deepEqual(input.original, { allOf: children })
})

for (const [kind, leaf] of [
  ['number', { operator: 'eq', value: 0 }],
  ['single', { operator: 'eqOption', value: 'sport' }],
  ['multi', { operator: 'containsAll', value: ['read', 'sport'] }],
]) {
  test(`${kind} wrapping and repeated group changes retain the configured answer`, () => {
    const input = editor(leaf, kind)
    assert.deepEqual(input.switch('anyOf'), { anyOf: [leaf] })
    assert.deepEqual(input.switch('allOf'), { allOf: [leaf] })
    assert.deepEqual(input.switch('anyOf'), { anyOf: [leaf] })
    assert.deepEqual(input.original, leaf)
  })
}
