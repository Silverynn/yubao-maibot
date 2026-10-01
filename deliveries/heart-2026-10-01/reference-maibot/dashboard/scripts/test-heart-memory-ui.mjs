// 不启动服务器或模型。使用真实 React/JSDOM 验证页面交互；只模拟后端和通用控件。
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { JSDOM } from 'jsdom'
import ts from 'typescript'

const require = createRequire(import.meta.url)
const dom = new JSDOM('<!doctype html><html><body></body></html>', { url: 'http://localhost/' })
globalThis.window = dom.window
globalThis.document = dom.window.document
Object.defineProperty(globalThis, 'navigator', { value: dom.window.navigator, configurable: true })
globalThis.HTMLElement = dom.window.HTMLElement
globalThis.IS_REACT_ACT_ENVIRONMENT = true
const React = require('react')
const { render, screen, fireEvent, waitFor, cleanup } = require('@testing-library/react')
const calls = []
const notices = []
let changed = 0
let pending = [{ id: 1, chat_id: 'group-1', name: '小明', text: '小明喜欢围棋', reason: '判断不确定',
  evidence: ['我喜欢围棋'], status: 'pending', created: 1000 }]
const toast = (value) => notices.push(value)
const passthrough = (tag) => ({ children, ...props }) => React.createElement(tag, props, children)
const mocks = {
  '@/components/ui/button': { Button: ({ children, variant, ...props }) => React.createElement('button', props, children) },
  '@/components/ui/textarea': { Textarea: passthrough('textarea') },
  '@/components/ui/dialog': {
    Dialog: ({ children, open }) => open ? React.createElement('div', { role: 'dialog' }, children) : null,
    DialogContent: passthrough('div'), DialogDescription: passthrough('p'), DialogFooter: passthrough('div'),
    DialogHeader: passthrough('div'), DialogTitle: passthrough('h2'),
  },
  '@/hooks/use-toast': { useToast: () => ({ toast }) },
  '@/lib/memory-api': { getMemoryImportChatTargets: async () => ({ success: true, data: [
    { chat_id: 'group-1', chat_name: '测试群', is_group: true },
  ] }) },
  '@/lib/http': { backendApi: { request: async (method, url, options) => {
    calls.push({ method, url, body: options?.body })
    if (url.includes('/delete')) { pending = []; return { success: true, message: '仅删除审核记录' } }
    if (url.includes('/review')) {
      pending = []; return { success: true, status: options.body.approve ? 'written' : 'ignored', message: '测试处理完成' }
    }
    if (url.includes('/group-edit/preview')) return { success: true, plan_id: 'plan-1', old_text: '本群旧摘要', new_text: options.body.new_text, message: '仅预览' }
    if (url.includes('/group-edit/execute')) return { success: true, message: '原生记忆已修正' }
    if (url.includes('/group-memories')) return { success: true, items: [
      { hash: 'old-hash', text: '本群旧摘要', type: 'chat_summary', created: 1000, name: '' },
    ] }
    const selectedStatus = new URL(url, 'http://localhost').searchParams.get('status')
    return { success: true, items: pending.filter((item) => !selectedStatus || item.status === selectedStatus) }
  } } },
}
const compiled = ts.transpileModule(readFileSync(new URL('../src/components/memory/HeartMemoryManager.tsx', import.meta.url), 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2022 },
}).outputText
const exports = {}
new Function('require', 'exports', compiled)((name) => mocks[name] ?? require(name), exports)
render(React.createElement(exports.HeartMemoryManager, { onChanged: () => { changed++ } }))
await screen.findByText('拟记住：小明喜欢围棋')
console.log('初始候选已加载')
assert.ok(screen.getByText('我喜欢围棋'))
fireEvent.change(screen.getByLabelText('候选状态'), { target: { value: 'written' } })
await waitFor(() => assert.equal(screen.queryByText('拟记住：小明喜欢围棋'), null))
console.log('状态切换完成')
await waitFor(() => assert.equal(screen.getByLabelText('候选状态').disabled, false))
fireEvent.change(screen.getByLabelText('候选状态'), { target: { value: 'pending' } })
await screen.findByText('拟记住：小明喜欢围棋')
fireEvent.click(screen.getByText('编辑后批准'))
fireEvent.change(screen.getByLabelText('新内容'), { target: { value: '小明喜欢研究围棋' } })
fireEvent.click(screen.getByText('批准修改后的内容'))
await waitFor(() => assert.equal(changed, 1))
const approval = calls.find((call) => call.url.includes('/review'))
assert.equal(approval.body.expected_text, '小明喜欢围棋')
assert.equal(approval.body.edited_text, '小明喜欢研究围棋')
assert.equal(approval.body.approve, true)
await waitFor(() => assert.equal(screen.queryByRole('dialog'), null))
fireEvent.change(screen.getByLabelText('群聊范围'), { target: { value: 'group-1' } })
await screen.findByText('本群旧摘要')
fireEvent.click(screen.getByText('修改原生记忆'))
fireEvent.change(screen.getByLabelText('新内容'), { target: { value: '本群新摘要' } })
fireEvent.click(screen.getByText('预览新旧内容'))
await screen.findByText('新内容：本群新摘要')
assert.equal(calls.filter((call) => call.url.includes('/execute')).length, 0)
fireEvent.click(screen.getByText('确认修改原生记忆'))
await waitFor(() => assert.equal(changed, 2))
const execution = calls.find((call) => call.url.includes('/execute'))
assert.deepEqual(execution.body, { plan_id: 'plan-1', confirmed: true })
assert.ok(notices.some((notice) => notice.title === '原生记忆已更新'))
pending = [{ id: 2, chat_id: 'group-1', name: '小红', text: '小红喜欢阅读', reason: '测试', evidence: [], status: 'pending', created: 1000 }]
fireEvent.click(screen.getByText('刷新'))
await screen.findByText('拟记住：小红喜欢阅读')
fireEvent.click(screen.getByText('删除候选／审核记录'))
assert.equal(calls.filter((call) => call.url.includes('/delete')).length, 0)
fireEvent.click(screen.getByText('确认删除审核记录'))
await waitFor(() => assert.equal(screen.queryByText('拟记住：小红喜欢阅读'), null))
assert.deepEqual(calls.find((call) => call.url.includes('/delete')).body, { expected_text: '小红喜欢阅读', confirmed: true })
cleanup()
console.log('PASS：证据展示、候选编辑审批、群选择、修改预览不执行、明确确认执行、刷新原生记忆视图')
