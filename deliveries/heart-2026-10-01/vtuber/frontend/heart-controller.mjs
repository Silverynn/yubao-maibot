// 纯逻辑层便于离线测试；稳定心情与话题情绪的来源只有 Heart 插件。
export class HeartController {
  constructor(report) { this.report = report; this.current = null; this.lastModel = null; this.lastCommand = null; }
  owns() { return Boolean(this.current?.ready && (this.current.enabled || this.current.manual)); }
  async update(state, adapter) {
    this.current = state;
    if (!state.ready) return;
    if (!state.valid_expression) { await this.report(state, 'failed'); return; }
    const model = adapter?.getModel?.();
    const expression = typeof state.expression === 'number'
      ? adapter?.getExpressionName?.(state.expression)
      : state.expression;
    // Open-LLM-VTuber models can expose either names or numeric indexes in
    // emotionMap. Never assume a particular model directory or expression set.
    if (!model || typeof expression !== 'string' || !expression ||
        (model._expressions?.getValue && model._expressions.getValue(expression) == null)) {
      await this.report(state, 'unavailable');
      return;
    }
    if (this.lastCommand === state.command_id && this.lastModel === model) {
      // 动作已完成但回执可能丢失：只重发回执，不重复播放动作。
      await this.report(state, 'applied');
      return;
    }
    try {
      model.setExpression(expression);
      this.lastModel = model;
      this.lastCommand = state.command_id;
    } catch (error) {
      await this.report(state, 'failed');
      throw error;
    }
    // 网络失败不等于表情执行失败，不能把两种情况混为一谈。
    await this.report(state, 'applied');
  }
}
