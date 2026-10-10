import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
const source=await readFile(new URL('../web/visual.js',import.meta.url),'utf8');
const {applyVisual}=await import('data:text/javascript;base64,'+Buffer.from(source).toString('base64'));
const current={},other={};let restored=0,applied=0;
const manager={expressions:[current,other],currentExpression:current,
  getExpressionIndex:name=>name==='neutral'?0:name==='happy'?1:-1,restoreExpression:()=>restored++};
const model={internalModel:{motionManager:{expressionManager:manager}},
  expression:async value=>{applied++;return value==='happy'||value===1;},motion:async()=>false};
assert.match(await applyVisual(model,'neutral',{group:'Idle',index:0}),/保持已有表情/);
assert.equal(restored,1);assert.equal(applied,0);
assert.match(await applyVisual(model,'happy',{group:'Happy',index:0}),/配套动作本次未启动/);
assert.match(await applyVisual(model,1),/已执行表情/);
await assert.rejects(()=>applyVisual(model,'missing'),/没有可执行/);
console.log('Live2D舞台语义检查：4项通过');
