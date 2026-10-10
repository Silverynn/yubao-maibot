// 测试主持人手动锁与麦克风锁的组合；没有真人麦克风或外部请求。
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
const code=await readFile(new URL('../web/author.js',import.meta.url),'utf8');
const {RoomEditor}=await import('data:text/javascript;base64,'+Buffer.from(code).toString('base64'));
const elements=new Map();
globalThis.document={getElementById:id=>{if(!elements.has(id))elements.set(id,{value:'',textContent:'',hidden:false});return elements.get(id);}};
const calls=[];
const editor=new RoomEditor({api:async(path,data)=>{calls.push({path,data});return {ok:true};},note:()=>{},refresh:async()=>{},context:()=>({room:'room-one',actor:{host:true}}),client:'browser-one'});
await editor.micSpeech(true);
assert.equal(calls.at(-1).data.speaking,true);
assert.equal(editor.manualHold,undefined);
await elements.get('host-hold').onclick();
assert.equal(editor.manualHold,true);
await editor.micSpeech(false);
assert.equal(calls.at(-1).data.speaking,true); // 结束识别不能解除仍在手动讲话的主持人。
await elements.get('host-hold').onclick();
assert.equal(editor.manualHold,false);
assert.equal(calls.at(-1).data.speaking,false);
assert.equal(editor.heldRoom,null);
await editor.hold(true);
await editor.micSpeech(true);
await editor.releaseAll();
assert.equal(editor.heldRoom,null);
assert.equal(editor.micHold,false);
assert.equal(editor.manualHold,false);
assert.equal(calls.at(-1).path,'/rooms/room-one/host-speech');
console.log('主持人手动讲话与麦克风锁、切换释放：11项通过');
