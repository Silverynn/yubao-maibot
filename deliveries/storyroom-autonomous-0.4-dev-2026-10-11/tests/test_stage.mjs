// 通过真实stage模块验证有限表情模型的降级和回执，渲染器模拟，不联网。
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
const visual=await readFile(new URL('../web/visual.js',import.meta.url),'utf8');
const visualUrl='data:text/javascript;base64,'+Buffer.from(visual).toString('base64');
const source=(await readFile(new URL('../web/stage.js',import.meta.url),'utf8')).replace("'./visual.js'",JSON.stringify(visualUrl));
const scene={clientWidth:800,clientHeight:500,dataset:{},style:{}};
const status={textContent:''};
globalThis.document={querySelector:selector=>selector==='#scene'?scene:status,
  createElement:()=>({}),head:{append:script=>queueMicrotask(()=>script.onload())}};
globalThis.ResizeObserver=class{observe(){}};
const expressions=['neutral','happy','smug'].map(Name=>({Name}));
const manager={expressions,currentExpression:expressions[0],getExpressionIndex:name=>expressions.findIndex(e=>e.Name===name),restoreExpression(){}};
const performed=[],loaded=[],destroyed=[];
const model={internalModel:{width:300,height:500,settings:{expressions},motionManager:{expressionManager:manager}},
  scale:{set(){}},anchor:{set(){}},destroy(){destroyed.push(true);},expression:async name=>{performed.push(name);manager.currentExpression=expressions.find(e=>e.Name===name);return !!manager.currentExpression;},motion:async()=>false};
globalThis.PIXI={Application:class{constructor(){this.renderer={width:800,height:500,resize(){}};this.stage={addChild(){},removeChild(){}};}},
  live2d:{Live2DModel:{from:async url=>{loaded.push(url);return {...model};}}}};
const {initStage,updateStage}=await import('data:text/javascript;base64,'+Buffer.from(source).toString('base64'));
const config={name:'partner-avatar',url:'/models/test.model3.json',emotionMap:{neutral:'neutral',joy:'happy',confusion:'absent'}};
const delivered={...config,name:'yubao-fullbody',url:'/models/yubao-fullbody/yubao-fullbody.model3.json'};
await initStage([config,delivered]);
const receipts=[];
const role={id:'partner',avatar:config.name,visual:{emotion:'生气',revision:1}};
const state={id:'isolated-room',stage:{scene:'harbor'},roles:[role]};
await updateStage(state,(...r)=>receipts.push(r));
assert.equal(receipts.at(-1)[1],'neutral');assert.match(receipts.at(-1)[3],/无专用表情/);
role.visual={emotion:'开心',revision:2};await updateStage(state,(...r)=>receipts.push(r));
assert.equal(performed.at(-1),'happy');assert.doesNotMatch(receipts.at(-1)[3],/无专用表情/);
role.visual={emotion:'疑惑',revision:3};await updateStage(state,(...r)=>receipts.push(r));
assert.equal(receipts.at(-1)[1],'neutral');assert.match(receipts.at(-1)[3],/无专用表情/);
role.avatar=delivered.name;await updateStage(state,(...r)=>receipts.push(r));
assert.equal(destroyed.length,1);assert.equal(loaded.at(-1),delivered.url);
assert.equal(receipts.length,4); // 角色ID和情绪revision没改，也必须在新模型上重新执行表情。
const {defaultAvatars}=await import('../web/avatar-defaults.js');
assert.deepEqual(defaultAvatars([{name:'moonbell-20261003'},{name:'ds-whale-girl'},{name:'yubao-fullbody'}]),['moonbell-20261003','yubao-fullbody']);
assert.deepEqual(defaultAvatars([{name:'partner-own'},{name:'partner-second'}]),['partner-own','partner-second']);
assert.deepEqual(defaultAvatars([]),['none','none']);
state.scene_image={status:'ready',image:'/api/scene-assets/'+ 'a'.repeat(64)+'.png'};await updateStage(state,(...r)=>receipts.push(r));assert.match(scene.style.backgroundImage,/scene-assets/);state.scene_image={status:'held',image:state.scene_image.image};await updateStage(state,(...r)=>receipts.push(r));assert.equal(scene.style.backgroundImage,'');
console.log('真实stage表情映射、换模释放、重新执行表情及默认模型兼容：12项断言通过');
