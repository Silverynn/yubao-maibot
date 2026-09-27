// Conversational expressions are brief reactions; the resting face is 平静.
export const EXPRESSION_HOLD_MS = 3000;
const reactions = new Set([
  "脸红", "爱心眼", "星星眼", "开心兴奋", "悲伤", "大哭", "生气",
  "晕晕", "阴暗", "调皮", "闭眼口水", "吐魂", "吐舌", "呆呆眼", "感叹号",
]);
const pending = new WeakMap();

export function yubaoSetTimedExpression(adapter, expression) {
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
    // A delayed reset must never affect a newly selected character.
    if (adapter.getModel() === model) model.setExpression("平静");
  }, EXPRESSION_HOLD_MS);
  pending.set(adapter, state);
}
