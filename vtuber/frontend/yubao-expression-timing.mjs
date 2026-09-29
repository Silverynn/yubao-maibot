// Conversational expressions are brief reactions; the resting face is 平静.
export const EXPRESSION_HOLD_MS = 3000;
const reactions = new Set([
  "脸红", "爱心眼", "星星眼", "开心兴奋", "悲伤", "大哭", "生气",
  "晕晕", "阴暗", "调皮", "闭眼口水", "吐魂", "吐舌", "呆呆眼", "感叹号", "问号",
]);
const pending = new WeakMap();

// 对话结束会自动重置表情；短暂反应仍在播放时，让计时器负责恢复。
// 用户明确选择“平静”仍走 setExpression，能够立即取消反应。
export function yubaoShouldDeferReset(adapter) {
  if (typeof window !== 'undefined' && window.yubaoHeartController?.owns()) return true;
  const state = adapter && pending.get(adapter);
  return Boolean(state && adapter.getModel() === state.model);
}

export function yubaoSetTimedExpression(adapter, expression) {
  // 接入 Heart 后由话题状态管理持久表情，旧音频/关键词路径不再抢控制权。
  if (typeof window !== 'undefined' && window.yubaoHeartController?.owns()) return;
  const previous = pending.get(adapter);
  if (previous) clearTimeout(previous.timer);
  pending.delete(adapter);

  const model = adapter.getModel();
  if (!model) return;
  model.setExpression(expression);
  if (!model._modelHomeDir?.includes("ds-whale-girl/") || !reactions.has(expression)) return;

  const state = { model, timer: null };
  state.timer = setTimeout(() => {
    if (pending.get(adapter) !== state) return;
    pending.delete(adapter);
    if (typeof window !== 'undefined' && window.yubaoHeartController?.owns()) return;
    // A delayed reset must never affect a newly selected character.
    if (adapter.getModel() === model) model.setExpression("平静");
  }, EXPRESSION_HOLD_MS);
  pending.set(adapter, state);
}
