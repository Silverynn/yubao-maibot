// 与舞台绘制分开，便于验证不同模型的名字/编号及“表情未变”返回值。
export async function applyVisual(model, expression, motion){
  const manager=model.internalModel.motionManager.expressionManager;
  const index=typeof expression==='number'?expression:manager?.getExpressionIndex(expression);
  const same=manager&&index>=0&&manager.expressions.indexOf(manager.currentExpression)===index;
  if(same)manager.restoreExpression();
  else if(!await model.expression(expression))throw Error('模型没有可执行的表情：'+String(expression));
  let status=same?'保持已有表情':'已执行表情';
  if(motion){const started=await model.motion(motion.group,motion.index,3);status+=started?'；配套动作已启动':'；配套动作本次未启动（可能已在播放或被占用）';}
  return status;
}
