import assert from 'node:assert/strict';
import {existsSync} from 'node:fs';
const local = new URL('./yubao-expression-timing.mjs', import.meta.url);
const {yubaoSetTimedExpression: set, yubaoShouldDeferReset: deferReset, EXPRESSION_HOLD_MS} = await import(
  existsSync(local) ? local : new URL('../frontend/yubao-expression-timing.mjs', import.meta.url)
);
let now = 0, nextId = 0;
const timers = new Map();
globalThis.setTimeout = (fn, delay) => { const id = ++nextId; timers.set(id, {fn, at: now + delay}); return id; };
globalThis.clearTimeout = id => timers.delete(id);
function advance(ms) {
  now += ms;
  for (const [id, job] of [...timers]) if (job.at <= now) { timers.delete(id); job.fn(); }
}
function model(dir = 'ds-whale-girl/') {
  return {_modelHomeDir: dir, calls: [], setExpression(name) { this.calls.push(name); }};
}
let current = model();
const adapter = {getModel: () => current};
set(adapter, '开心兴奋');
assert.equal(deferReset(adapter), true, 'conversation-end reset must wait for reaction');
advance(EXPRESSION_HOLD_MS - 1);
assert.deepEqual(current.calls, ['开心兴奋']);
advance(1);
assert.deepEqual(current.calls, ['开心兴奋', '平静']);
assert.equal(deferReset(adapter), false);
current = model();
set(adapter, '生气'); advance(2000); set(adapter, '悲伤'); advance(1000);
assert.deepEqual(current.calls, ['生气', '悲伤']);
advance(2000);
assert.deepEqual(current.calls, ['生气', '悲伤', '平静']);
current = model();
set(adapter, '感叹号'); set(adapter, '平静'); advance(4000);
assert.equal(deferReset(adapter), false, 'explicit neutral cancels the reaction');
assert.deepEqual(current.calls, ['感叹号', '平静']);
set(adapter, '生气'); const old = current; current = model(); advance(4000);
assert.equal(deferReset(adapter), false, 'do not defer reset for a new model');
assert.deepEqual(current.calls, []);
assert.equal(old.calls.at(-1), '生气');
current = model('other-character/'); set(adapter, '生气'); advance(4000);
assert.deepEqual(current.calls, ['生气']);
current = null; set(adapter, '平静');
console.log('PASS: 3s reset, timer replacement, neutral cancellation, model switching, unrelated models');
current = model(); set(adapter, '生气');
globalThis.window = {yubaoHeartController:{owns:()=>true}};
advance(4000);
assert.deepEqual(current.calls,['生气'],'Heart接管后旧计时器不能重置表情');
assert.equal(deferReset(adapter),true,'话题情绪不随一轮对话结束而清空');
set(adapter,'平静');
assert.deepEqual(current.calls,['生气'],'旧路径不得覆盖Heart状态');
delete globalThis.window;
console.log('PASS: Heart ownership blocks legacy timer and end-of-turn reset');
