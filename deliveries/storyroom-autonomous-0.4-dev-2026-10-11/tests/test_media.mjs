// 模拟DOM与系统声音，只测试控制逻辑；不打开麦克风、不播放真实声音。
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
const audio=await readFile(new URL('../web/audio.js',import.meta.url),'utf8');
const url='data:text/javascript;base64,'+Buffer.from(audio).toString('base64');
const source=(await readFile(new URL('../web/media.js',import.meta.url),'utf8')).replace("'./audio.js'",JSON.stringify(url));
const {RoomMedia}=await import('data:text/javascript;base64,'+Buffer.from(source).toString('base64'));
class Element{
  constructor(){this.children=[];this.value='';this.checked=false;this.hidden=false;this.disabled=false;}
  get options(){return this.children;}
  append(...items){this.children.push(...items);if(!this.value&&items[0]?.value)this.value=items[0].value;}
  replaceChildren(){this.children=[];this.value='';}
}
const elements=new Map();globalThis.document={getElementById:id=>{if(!elements.has(id))elements.set(id,new Element());return elements.get(id);},createElement:()=>new Element(),createTextNode:text=>text};
const local={localService:true,name:'本地',voiceURI:'local',lang:'zh-CN'},cloud={localService:false,name:'云端',voiceURI:'cloud',lang:'zh-CN'};
const spoken=[],calls=[];let stopped=0;
globalThis.speechSynthesis={getVoices:()=>[local,cloud],addEventListener:()=>{},speak:u=>spoken.push(u),cancel:()=>stopped++};
globalThis.SpeechSynthesisUtterance=class{constructor(text){this.text=text;}};
const api=async(path,data)=>{calls.push({path,data});if(path==='/media')return {asr_configured:true};if(path.endsWith('/turn'))return {task_id:'new-task'};return {ok:true};};
const media=new RoomMedia({api,note:()=>{},refresh:async()=>{},client:'test-browser'});
await media.configure(true);
assert.equal(elements.get('realtime').checked,false);
assert.deepEqual(elements.get('voice-select').options.map(o=>o.value),['local']);
const roles=[{id:'a',name:'甲'},{id:'b',name:'乙'}];
let state={id:'room',roles,version:0,tasks:[]};media.update(state);
const control=media.roleControl(roles[0]);control.children[0].checked=true;control.children[0].onchange();
assert.equal(media.enabled.get('a'),true);assert.equal(media.enabled.get('b'),undefined);
const first={id:'one',role:'a',version:0,status:'proposed',result:{speech:'我检查灯塔'}},second={id:'two',role:'b',version:0,status:'proposed',result:{speech:'我不应朗读'}};
state={...state,tasks:[second,first]};media.update(state);media.update(state);
await new Promise(resolve=>setImmediate(resolve)); // 发言须先取得服务端房间锁。
assert.equal(spoken.length,1);assert.equal(spoken[0].text,'我检查灯塔');
spoken[0].onstart();media.ownTask='one';await media.bargeIn(media.epoch);
assert.equal(media.playing,null);assert.ok(stopped>0);assert.ok(calls.some(c=>c.path.endsWith('/one/cancel')));
const before=calls.filter(c=>c.path.endsWith('/turn')).length;await media.submit('停一下！',media.epoch);
assert.equal(calls.filter(c=>c.path.endsWith('/turn')).length,before);
await media.submit('请检查灯塔',media.epoch);assert.equal(media.ownTask,'new-task');
assert.equal(calls.find(c=>c.path.endsWith('/turn')).data.role,'a');
const epoch=media.epoch;media.reset('关闭');await media.submit('过期输入',epoch);
assert.equal(calls.filter(c=>c.path.endsWith('/turn')).length,before+1);
assert.equal(elements.get('realtime').checked,false);
assert.ok(calls.some(c=>c.data?.feature==='播放停止'&&c.data.text==='我检查灯塔'));
assert.ok(calls.some(c=>c.data?.feature==='语音开关'&&c.data.enabled));
assert.ok(calls.some(c=>c.path.endsWith('/audio-lease')&&c.data.playing));
assert.ok(calls.some(c=>c.path.endsWith('/audio-lease')&&!c.data.playing));
assert.ok(calls.some(c=>c.path.endsWith('/host-speech')&&c.data.speaking));
assert.ok(calls.some(c=>c.path.endsWith('/dialogue')&&c.data.text==='请检查灯塔'));
console.log('浏览器语音、目标隔离、取消、快反应、发言锁及日志：20项通过');

media.queue=[{role:'a',task:'one',text:'不应播放'}];media.update({...media.state,runtime:{enabled:1,state:'PAUSED'}});assert.equal(media.queue.length,0);assert.equal(media.playing,null);
