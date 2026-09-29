import {HeartController} from './heart-controller.mjs';

const client = crypto.randomUUID();
const acknowledged = new Set();
async function report(state, status) {
  const key = state.command_id + ':' + status;
  if (acknowledged.has(key)) return;
  const result = await fetch('/heart/avatar-events', {
    method: 'POST', headers: {'Content-Type':'application/json', 'X-Heart-Client':state.csrf},
    body: JSON.stringify({command_id:state.command_id, client_id:client, status}),
    signal: AbortSignal.timeout(3000),
  });
  if (!result.ok) throw Error('动作日志回执未保存');
  acknowledged.add(key);
  if (acknowledged.size > 256) acknowledged.delete(acknowledged.values().next().value);
}
const controller = new HeartController(report);
window.yubaoHeartController = controller;
const panel = document.createElement('section');
panel.id = 'heart-mood-panel';
panel.setAttribute('aria-label', '鱼宝当前心情与话题情绪');
panel.style.cssText = 'position:fixed;right:18px;top:16px;z-index:1000;width:260px;max-width:calc(100vw - 36px);padding:14px 18px;color:#fff;background:rgba(15,23,42,.9);border:1px solid #64748b;border-radius:14px;font:14px/1.6 system-ui;pointer-events:none;box-shadow:0 4px 20px #0004';
const heading = document.createElement('div'); heading.style.cssText='color:#cbd5e1;font-size:12px';
const score = document.createElement('div'); score.style.cssText='font-size:23px;font-weight:600';
const emotion = document.createElement('div');
const topic = document.createElement('div'); topic.style.cssText='font-size:12px;color:#cbd5e1;overflow-wrap:anywhere';
const manual = document.createElement('div'); manual.style.cssText='font-size:12px;color:#fde68a';
const status = document.createElement('div'); status.style.cssText='font-size:11px;color:#a5b4fc;margin-top:5px';
heading.textContent='Heart · 当前网页会话'; score.textContent='心情 -- / 100'; status.textContent='正在连接心情插件…';
panel.append(heading,score,emotion,topic,manual,status);
document.body.appendChild(panel);

async function poll() {
  try {
    const response = await fetch('/heart/state', {cache:'no-store', signal:AbortSignal.timeout(3000)});
    if (!response.ok) throw Error('心情接口未连接');
    const state = await response.json();
    if (state.ready) {
      heading.textContent='Heart · ' + state.session_name;
      score.textContent='心情 ' + (typeof state.value === 'number' ? state.value.toFixed(1).replace(/\.0$/,'') : '--') + ' / 100 · ' + state.band;
      emotion.textContent=state.active ? '当前情绪：' + state.label + ' · 强度 ' + Math.round(state.intensity*100) + '%' : '当前情绪：无额外的小情绪';
      topic.textContent=state.active ? '话题：' + state.topic : '临时情绪结束后，恢复心情对应的常态';
      manual.textContent=state.manual ? '手动表情：' + state.manual_expression + '（暂时展示，不改变心情）' : state.performance ? '应请求表演：' + state.performance_expression + '（与真实情绪分开）' : '';
      status.textContent=state.enabled ? state.notice + ' · ' + state.updated : '心情插件已关闭 · 保留上次记录';
    } else {
      status.textContent=state.notice;
    }
    await controller.update(state, window.getLAppAdapter?.());
  } catch (error) {
    status.textContent='同步或动作回执未成功 · 保留上次已确认显示，稍后重试';
  } finally {
    setTimeout(poll,1000);
  }
}
poll();
