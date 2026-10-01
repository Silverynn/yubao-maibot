// 不启动构建子进程：使用真实 React/JSDOM 测试 hook 和来源表格，仅替换后台与通用控件。
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { JSDOM } from 'jsdom'
import ts from 'typescript'

const require = createRequire(import.meta.url)
const dom = new JSDOM('<!doctype html><html><body></body></html>', { url: 'http://localhost/plugin-config' })
globalThis.window = dom.window
globalThis.document = dom.window.document
globalThis.HTMLElement = dom.window.HTMLElement
Object.defineProperty(globalThis, 'navigator', { value: dom.window.navigator, configurable: true })
globalThis.IS_REACT_ACT_ENVIRONMENT = true
const React = require('react')
const { renderHook, render, screen, cleanup, waitFor } = require('@testing-library/react')
const plugins = ['not_started', 'unknown', 'inactive', 'failed', 'success', 'loading', 'offline'].map((status) => ({
  id: status, path: `/plugins/${status}`, enabled: true, load_status: status,
  manifest: { id: status, name: status, version: '1.0.0', manifest_version: 2 },
}))
const mocks = {
  '@/lib/plugin-api': {
    getInstalledPlugins: async () => plugins, fetchPluginList: async () => [],
    getMaimaiVersion: async () => ({ version: '1.2.5' }), isPluginCompatible: () => true,
  },
  '@/hooks/use-toast': { useToast: () => ({ toast: () => {} }) },
  '../../plugins/types': { getPluginType: () => 'normal' },
  '../utils': { getPluginConfigRoutePath: () => '/plugin-config', isAdapterManagementPath: () => false },
}
function load(file, overrides) {
  const compiled = ts.transpileModule(readFileSync(new URL(file, import.meta.url), 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2022 },
  }).outputText
  const exports = {}
  new Function('require', 'exports', compiled)((name) => overrides[name] ?? require(name), exports)
  return exports
}
const { usePluginList } = load('../src/routes/plugin-config/hooks/usePluginList.ts', mocks)
const hook = renderHook(() => usePluginList())
await waitFor(() => assert.equal(hook.result.current.installedCount, 7))
assert.equal(hook.result.current.loadFailedCount, 1)
assert.equal(hook.result.current.getPluginStatusLabel(plugins[0]), '运行时未启动')
assert.equal(hook.result.current.getPluginStatusMeta(plugins[1]).label, '状态待确认')
assert.equal(hook.result.current.getPluginStatusLabel(plugins[2]), '未激活')
assert.equal(hook.result.current.getPluginStatusMeta(plugins[3]).label, '加载失败')
assert.equal(hook.result.current.visiblePlugins.length, 7)
assert.ok(hook.result.current.modernLoadSummaryLabel.includes('未启动 1 个'))
cleanup()
console.log('插件状态：未启动/未知/未激活不算失败；明确失败、加载中、离线、成功均保留，7张卡片无丢失。')

const uiMocks = { '@/lib/utils': { cn: (...args) => args.filter(Boolean).join(' ') },
  '../constants': { DELETE_OPERATION_ITEM_PAGE_SIZE: 10, DELETE_OPERATION_PAGE_SIZE: 10 }, '../utils': {}, }
for (const [module, names, tag] of [
  ['alert', ['Alert', 'AlertDescription'], 'div'], ['badge', ['Badge'], 'span'], ['button', ['Button'], 'button'],
  ['card', ['Card', 'CardContent', 'CardDescription', 'CardHeader', 'CardTitle'], 'div'],
  ['checkbox', ['Checkbox'], 'input'], ['input', ['Input'], 'input'], ['label', ['Label'], 'label'],
  ['scroll-area', ['ScrollArea'], 'div'], ['select', ['Select', 'SelectContent', 'SelectItem', 'SelectTrigger', 'SelectValue'], 'div'],
  ['table', ['Table'], 'table'], ['table', ['TableBody'], 'tbody'], ['table', ['TableCell'], 'td'],
  ['table', ['TableHead'], 'th'], ['table', ['TableHeader'], 'thead'], ['table', ['TableRow'], 'tr'],
  ['tabs', ['TabsContent'], 'div'], ['thinking-illustration', ['ThinkingIllustration'], 'div'],
]) {
  const key = `@/components/ui/${module}`
  uiMocks[key] ??= {}
  for (const name of names) uiMocks[key][name] = ({ children, disabled }) => React.createElement(tag, { disabled }, children)
}
const { DeleteTab } = load('../src/routes/resource/knowledge-base/tabs/DeleteTab.tsx', uiMocks)
const state = {
  sourceSearch: '', selectedSources: [], filteredSources: [
    { source: '完整统计', paragraph_count: 7, relation_count: 3 },
    { source: '旧接口', count: 4 }, { source: '真正为零', paragraph_count: 1, relation_count: 0 },
  ],
  filteredDeleteOperations: [], deleteOperations: [], pagedDeleteOperations: [], selectedDeleteOperation: null,
  selectedOperationSources: [], selectedOperationItems: [], filteredSelectedOperationItems: [], pagedSelectedOperationItems: [],
  selectedOperationCounts: {}, operationPage: 1, deleteOperationPageCount: 1, selectedOperationItemPage: 1,
  selectedOperationItemPageCount: 1,
}
render(React.createElement(DeleteTab, { delete: state }))
const rows = ['完整统计', '旧接口', '真正为零'].map((label) => screen.getByText(label).closest('tr').textContent)
assert.equal(rows[0], '完整统计73')
assert.equal(rows[1], '旧接口4待统计')
assert.equal(rows[2], '真正为零10')
cleanup()
console.log('来源表格：显示真实数量，兼容 count 字段，未知显示待统计，真实0仍显示0。')
