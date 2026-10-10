// 两个模型共享一个透明画布。只接收房间公开的表情，不连接各自 QQ 会话。
import {applyVisual} from './visual.js';
const emotionKey={平静:'neutral',开心:'joy',疑惑:'confusion',生气:'anger',难过:'sadness',惊讶:'surprise',害羞:'blush',思考:'thinking'};
let app=null, catalog=[], models=new Map(), revision=new Map(), loading=new Map();
const stageErrors=new Map();
function script(url){return new Promise((resolve,reject)=>{const s=document.createElement('script');s.src=url;s.onload=resolve;s.onerror=()=>reject(Error('舞台库加载失败：'+url));document.head.append(s);});}
export async function initStage(modelCatalog){
  catalog=modelCatalog;
  if(app)return;
  if(!catalog.length){document.querySelector('#renderer-status').textContent='未指定模型目录，房间与投骰仍可使用';return;}
  await script('/static/vendor/pixi.min.js');
  await script('/core/live2dcubismcore.min.js');
  await script('/static/vendor/cubism4.min.js');
  const container=document.querySelector('#scene');
  app=new PIXI.Application({view:document.querySelector('#avatars'),backgroundAlpha:0,antialias:true,resolution:1});
  new ResizeObserver(()=>place()).observe(container);
}
function place(){
  if(!app)return;
  // 登录前舞台被 hidden，初始化尺寸是0。显示后必须真正重设绘图缓冲区，
  // 不能只靠 CSS 把一个0像素画布拉大，否则模型加载成功也完全看不见。
  const container=document.querySelector('#scene'),width=container.clientWidth,height=container.clientHeight;
  if(!width||!height)return;
  if(app.renderer.width!==width||app.renderer.height!==height)app.renderer.resize(width,height);
  const ordered=[...models.values()];
  ordered.forEach((model,i)=>{
    const scale=Math.min(app.renderer.height*.68/model.internalModel.height,app.renderer.width*.48/model.internalModel.width);
    model.scale.set(scale);model.anchor.set(.5,1);model.x=app.renderer.width*(i===0?.28:.72);model.y=app.renderer.height*.74;
  });
}
async function load(role){
  if(models.has(role.id)||loading.has(role.id))return;
  const config=catalog.find(c=>c.name===role.avatar);
  if(!config)throw Error('当前平台没有玩家的模型：'+role.avatar);
  const promise=PIXI.live2d.Live2DModel.from(config.url,{autoInteract:false,idleMotionGroup:'Idle'});
  loading.set(role.id,promise);
  try{const model=await promise;app.stage.addChild(model);models.set(role.id,model);place();}
  finally{loading.delete(role.id);}
}
export async function updateStage(state,receipt){
  document.querySelector('#scene').dataset.scene=state.stage.scene;
  if(!app)return;
  const ids=new Set(state.roles.map(r=>r.id));
  for(const [id,model] of models)if(!ids.has(id)){app.stage.removeChild(model);model.destroy();models.delete(id);revision.delete(id);stageErrors.delete(id);}
  for(const role of state.roles){
    try{
      await load(role);
      const model=models.get(role.id),config=catalog.find(c=>c.name===role.avatar);
      const label=role.visual.emotion||'平静',expression=config.emotionMap[emotionKey[label]]??config.emotionMap.neutral;
      const key=state.id+role.id+role.visual.revision;
      if(revision.get(role.id)===key)continue;
      let actual=expression;
      if(typeof actual==='string'&&!model.internalModel.settings.expressions?.some(e=>e.Name===actual||e.name===actual)){
        // 模型没有思考等表情时明确记为未支持，不伪造已执行。
        actual=config.emotionMap.neutral;
      }
      const motion=config.expressionMotions?.[actual];
      const result=await applyVisual(model,actual,motion);
      revision.set(role.id,key);
      stageErrors.delete(role.id);
      await receipt(role.id,actual,label,(expression===actual?'':'此分类无专用表情，明确展示中性；')+result);
    }catch(e){stageErrors.set(role.id,e.message);document.querySelector('#renderer-status').textContent=e.message;await receipt(role.id,'',role.visual.emotion,e.message);}
  }
  if(models.size===state.roles.length)document.querySelector('#renderer-status').textContent=stageErrors.size?[...stageErrors.values()].join('；'):'双角色 Live2D · 同一舞台';
}
