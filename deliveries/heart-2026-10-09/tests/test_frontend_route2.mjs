import assert from 'node:assert/strict';
class Element extends EventTarget {
  constructor(){super();this.attrs=new Map();this.style={};this.textContent='';}
  setAttribute(k,v){this.attrs.set(k,v);} getAttribute(k){return this.attrs.get(k);}
}
class AudioElement extends EventTarget {
  constructor(){super();this.currentTime=0;this.paused=false;}
  play(){this.dispatchEvent(new Event('playing'));return Promise.resolve();} pause(){this.paused=true;}
}
class FakeWebSocket extends EventTarget {
  static OPEN=1;
  constructor(url='ws://127.0.0.1:12393/client-ws'){super();this.url=url;this.readyState=1;this.sent=[];}
  send(raw){this.sent.push(JSON.parse(raw));}
  receive(p){this.dispatchEvent(new MessageEvent('message',{data:JSON.stringify(p)}));}
}
const elements=[];
globalThis.window=new EventTarget();globalThis.WebSocket=FakeWebSocket;globalThis.HTMLMediaElement=AudioElement;
globalThis.location={href:'http://127.0.0.1:5173/',hostname:'127.0.0.1',port:'5173'};
globalThis.document={createElement:()=>new Element(),getElementById:()=>null,body:{appendChild:e=>elements.push(e)}};
globalThis.requestAnimationFrame=fn=>fn();
await import('../vt/frontend/heart-realtime.mjs');
const ws=new WebSocket();let checks=0;
function check(label,fn){fn();checks++;}
ws.send(JSON.stringify({type:'text-input',text:'普通消息'}));
const rt=elements.find(e=>e.id==='heart-realtime-toggle'),early=elements.find(e=>e.id==='heart-preemptive-toggle');
check('默认关闭，无额外实时输入',()=>assert.equal(ws.sent.length,1));
check('外置两个开关',()=>assert.ok(rt&&early));
check('说明采用用户指定原文',()=>assert.equal(early.title,'在你说话时就开始生成回答，加快回答速度但可能增加模型用量'));
check('默认开关ARIA状态',()=>assert.equal(early.getAttribute('aria-pressed'),'false'));
early.onclick();
check('开启后的提示采用用户指定原文',()=>assert.equal(elements.find(e=>e.id==='heart-realtime-status').textContent,early.title));
check('路线2开启会同时启用实时插件',()=>assert.deepEqual(ws.sent.at(-1),{type:'heart-rt',action:'mode',enabled:true,preemptive:true}));
ws.receive({type:'heart-rt-mode',enabled:true,preemptive:true});
check('界面显示路线2已开启',()=>assert.equal(early.textContent,'⚡ 提前生成：开启'));
window.heartRealtime.begin();let utterance=ws.sent.findLast(p=>p.action==='begin').utterance_id;
const n=ws.sent.length;early.onclick();
check('说话期间不切换生成策略',()=>assert.equal(ws.sent.length,n));
ws.receive({type:'heart-rt-stop',epoch:1});ws.receive({type:'heart-rt-bound',epoch:1,utterance_id:utterance});
const clip={clip_id:'a',epoch:1},audio=new AudioElement();window.heartRealtime.attach(audio,clip);await audio.play();
check('播放回执',()=>assert.equal(ws.sent.at(-1).phase,'playing'));
window.heartRealtime.end();ws.send(JSON.stringify({type:'mic-audio-end'}));
check('正式语音来源编号',()=>assert.equal(ws.sent.at(-1).heart_rt_utterance,utterance));
ws.send(JSON.stringify({type:'text-input',text:'换个问题'}));const newer=ws.sent.at(-1).heart_rt_utterance;
check('新输入立即停止旧声',()=>assert.equal(audio.paused,true));
ws.receive({type:'heart-rt-active',epoch:1,utterance_id:utterance});
await assert.rejects(()=>audio.play(),e=>e.name==='AbortError');checks++;
ws.receive({type:'heart-rt-stop',epoch:2});ws.receive({type:'heart-rt-active',epoch:2,utterance_id:newer});
check('旧片段被拒绝',()=>assert.equal(window.heartRealtime.acceptTask({heartRt:clip}),false));
window.heartRealtime.textShown({clip_id:'b',epoch:2});
check('纯文字回执',()=>assert.equal(ws.sent.at(-1).phase,'visible'));
early.onclick();check('可单独关闭提前生成',()=>assert.equal(ws.sent.at(-1).preemptive,false));
rt.onclick();check('插件关闭被上报',()=>assert.equal(ws.sent.at(-1).enabled,false));
ws.receive({type:'heart-rt-mode',enabled:false,preemptive:false});
ws.send(JSON.stringify({type:'text-input',text:'回到普通模式'}));
check('关闭后正式消息无实时编号',()=>assert.equal(ws.sent.at(-1).heart_rt_utterance,undefined));
const ordinary=new AudioElement();await ordinary.play();checks++;
check('任意模型的表情动作不被实时模块修改',()=>{
  for(const name of ['角色甲','角色乙']){
    const payload={model_info:{name},actions:{expressions:[name+'自己的表情'],motions:['自己的动作']}};
    const before=structuredClone(payload);
    assert.equal(window.heartRealtime.accept(payload),true);assert.deepEqual(payload,before);
  }
});
const external=new WebSocket('wss://other.example/client-ws');
external.send(JSON.stringify({type:'text-input',text:'其他服务的消息'}));
check('其他主机的WebSocket不被改写',()=>assert.equal(external.sent.at(-1).heart_rt_utterance,undefined));
check('前后端不同端口仍可接入',()=>assert.ok(ws.sent.some(p=>p.action==='begin')));
console.log(JSON.stringify({scope:'模拟DOM，不是真实麦克风/扬声器',checks}));
