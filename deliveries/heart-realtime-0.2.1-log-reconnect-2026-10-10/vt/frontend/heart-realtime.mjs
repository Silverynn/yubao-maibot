// 只接入原生 VAD 的帧和原生播放队列；策略与检索均在 MaiBot 插件。
export class Fence {
  constructor(){this.epoch=0;this.stopped=false;this.connection='';}
  advance(epoch){if(Number.isInteger(epoch)&&epoch>this.epoch){this.epoch=epoch;this.stopped=true;}}
  accepts(packet){return !packet?.heart_rt || (packet.heart_rt.epoch===this.epoch&&!this.stopped&&
    (!this.connection||packet.heart_rt.connection_id===this.connection));}
}

if(typeof window!=='undefined') {
  const fence=new Fence(), originalSend=WebSocket.prototype.send;
  let socket,utterance='',listening=false,enabled=false,preemptive=false,frames=[],ring=[],status,discardMicEnd=false;
  const audioClips=new WeakMap(), active=new Set();
  function show(text){
    if(!status){status=document.createElement('div');status.id='heart-realtime-status';
      status.setAttribute('role','status');status.style.cssText='position:fixed;left:18px;bottom:24px;z-index:1003;color:white;background:#0f172ae6;border-radius:10px;padding:10px;font:14px system-ui;max-width:350px';
      document.body.appendChild(status);}
    status.textContent=text;
  }
  function send(action,data={}){
    if(socket?.readyState===WebSocket.OPEN) originalSend.call(socket,JSON.stringify({type:'heart-rt',action,...data}));
  }
  function stopLocal(){
    fence.stopped=true;
    for(const audio of active){sendReceipt(audio,'stopped');audio.pause();}
    active.clear();
    window.dispatchEvent(new CustomEvent('heart-speech',{detail:'ended'}));
  }
  function sendReceipt(audio,phase){
    const packet=audioClips.get(audio);if(!packet)return;
    send('receipt',{clip_id:packet.clip_id,epoch:packet.epoch,phase,position:Number.isFinite(audio.currentTime)?audio.currentTime:0});
  }
  function voiceEnabled(){return document.getElementById('heart-voice-toggle')?.getAttribute('aria-pressed')!=='false';}
  const control=document.createElement('button');control.type='button';
  control.id='heart-realtime-toggle';control.setAttribute('aria-pressed','false');
  control.textContent='☎ 实时插件：关闭';control.style.cssText='position:fixed;left:18px;bottom:126px;z-index:1003;padding:10px;border-radius:10px;background:#164e63;color:white;border:1px solid #67e8f9;cursor:pointer';
  const early=document.createElement('button');early.type='button';early.id='heart-preemptive-toggle';
  early.setAttribute('aria-pressed','false');early.textContent='⚡ 提前生成：关闭';
  const earlyDescription='在你说话时就开始生成回答，加快回答速度但可能增加模型用量';
  early.title=earlyDescription;
  early.style.cssText='position:fixed;left:18px;bottom:76px;z-index:1003;padding:10px;border-radius:10px;background:#164e63;color:white;border:1px solid #67e8f9;cursor:pointer';
  function updateControls(){control.textContent=enabled?'☎ 实时插件：开启':'☎ 实时插件：关闭';
    control.setAttribute('aria-pressed',String(enabled));early.textContent=preemptive?'⚡ 提前生成：开启':'⚡ 提前生成：关闭';early.setAttribute('aria-pressed',String(preemptive));}
  control.onclick=()=>{enabled=!enabled;if(!enabled){preemptive=false;stopLocal();listening=false;frames=[];}
    updateControls();send('mode',{enabled,preemptive});
    show(enabled?'实时插件已开启。可另行开启提前生成，然后使用原麦克风按钮。':'实时插件已关闭；下一轮按普通模式处理。');};
  early.onclick=()=>{if(listening){show('请在本轮说完后切换提前生成。');return;}
    preemptive=!preemptive;if(preemptive)enabled=true;updateControls();send('mode',{enabled,preemptive});
    show(preemptive?earlyDescription:'提前生成已关闭；实时打断和查询仍可使用。');};
  document.body.appendChild(control);
  document.body.appendChild(early);
  const nativePlay=HTMLMediaElement.prototype.play;
  HTMLMediaElement.prototype.play=function(...args){
    const packet=audioClips.get(this);
    if(packet){
      if(packet.epoch!==fence.epoch||fence.stopped)return Promise.reject(new DOMException('旧输出已取消','AbortError'));
      if(!active.has(this)){
        active.add(this);
        this.addEventListener('playing',()=>sendReceipt(this,'playing'),{once:true});
        this.addEventListener('timeupdate',()=>sendReceipt(this,'progress'));
        this.addEventListener('ended',()=>{sendReceipt(this,'completed');active.delete(this);},{once:true});
        this.addEventListener('error',()=>{sendReceipt(this,'failed');active.delete(this);},{once:true});
      }
    }
    return nativePlay.apply(this,args);
  };
  // main 接缝在进入原生字幕、表情和播放队列前调用；失败会被原队列处理。
  window.heartRealtime={
    attach(audio,packet){if(packet)audioClips.set(audio,packet);},
    acceptTask(options){return fence.accepts({heart_rt:options.heartRt});},
    textShown(packet){if(packet)requestAnimationFrame(()=>send('receipt',{
      clip_id:packet.clip_id,epoch:packet.epoch,phase:'visible',position:0}));},
    frame(probs,audio){
      if(!enabled||!audio?.length)return;
      const copy=Array.from(audio);ring.push(...copy);if(ring.length>6400)ring.splice(0,ring.length-6400);
      if(listening){frames.push(...copy);if(frames.length>=2048){send('audio',{utterance_id:utterance,samples:frames.splice(0,4096)});}}
    },
    begin(){
      if(!enabled)return;
      stopLocal();utterance=crypto.randomUUID().replaceAll('-','');listening=true;
      frames=ring.slice();ring=[];
      send('begin',{utterance_id:utterance,audio:true,voice:voiceEnabled()});
      if(frames.length)send('audio',{utterance_id:utterance,samples:frames.splice(0)});
      show('正在听…后台可以查记忆，你可以继续说。');
    },
    end(){if(!enabled||!listening)return; if(frames.length)send('audio',{utterance_id:utterance,samples:frames.splice(0)});
      listening=false;send('input_end',{utterance_id:utterance});},
    accept(packet){
      if(!fence.accepts(packet))return false;
      return true;
    },
    get enabled(){return enabled;}
  };
  WebSocket.prototype.send=function(raw){
    const url=new URL(this.url,location.href);
    const aliases=new Set(['localhost','127.0.0.1','[::1]']);
    if(url.pathname!=='/client-ws'||
       !(url.hostname===location.hostname||(aliases.has(url.hostname)&&aliases.has(location.hostname)))||
       typeof raw!=='string')return originalSend.call(this,raw);
    if(socket!==this){
      socket=this;
      if(enabled)send('mode',{enabled,preemptive});
      this.addEventListener('message',e=>{try{
        const p=JSON.parse(e.data);
        if(p.type==='heart-rt-link-lost'){stopLocal();show(p.text);}
        if(p.type==='heart-rt-bound'&&p.new_connection&&p.utterance_id===utterance){
          stopLocal();fence.connection=p.connection_id;fence.epoch=p.epoch;fence.stopped=false;}
        if(p.connection_id&&fence.connection&&p.connection_id!==fence.connection)return;
        if(p.type==='heart-rt-mode'){enabled=p.enabled===true;preemptive=enabled&&p.preemptive===true;updateControls();}
        if(p.type==='heart-rt-stop'){fence.advance(p.epoch);stopLocal();show('已停止旧输出，正在听或核实。');}
        if(['heart-rt-bound','heart-rt-active'].includes(p.type)&&p.utterance_id===utterance&&p.epoch>=fence.epoch){fence.epoch=p.epoch;fence.stopped=false;}
        if(p.type==='heart-rt-partial'&&p.epoch===fence.epoch)show('临时听到：'+p.text);
        if(['heart-rt-state','heart-rt-ack','heart-rt-clarify'].includes(p.type)&&p.epoch===fence.epoch)show(p.text);
        if(p.type==='heart-rt-correction'&&p.utterance_id===utterance&&p.epoch>=fence.epoch){fence.epoch=p.epoch;fence.stopped=false;show(p.evidence?.mode==='supplement'?'查询结果已返回，正在补充…':'新的查询依据已到达，正在核实并更正…');}
      }catch{}});
    }
    try{
      const p=JSON.parse(raw);
      if(enabled&&p.type==='mic-audio-end'){
        if(discardMicEnd){discardMicEnd=false;listening=false;frames=[];return;}
        window.heartRealtime.end();p.heart_rt_utterance=utterance;raw=JSON.stringify(p);
      }
      if(p.type==='mic-audio-data'&&discardMicEnd)return;
      if(enabled&&p.type==='text-input'){
        discardMicEnd=listening;listening=false;frames=[];
        stopLocal();utterance=crypto.randomUUID().replaceAll('-','');
        send('begin',{utterance_id:utterance,audio:false,voice:voiceEnabled()});
        p.heart_rt_utterance=utterance;raw=JSON.stringify(p);
      }
      if(enabled&&p.type==='interrupt-signal'&&!listening){stopLocal();send('stop');}
    }catch{}
    return originalSend.call(this,raw);
  };
}
