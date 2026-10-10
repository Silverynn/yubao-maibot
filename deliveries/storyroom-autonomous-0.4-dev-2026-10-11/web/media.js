import {wavBase64,fastSignal} from './audio.js';

// 设置仅作用于此浏览器。未接入原VT实时插件，不能冒充提前生成或晚到证据纠正。
export class RoomMedia {
  constructor({api,note,refresh,client,hostSpeech}) {
    Object.assign(this,{api,note,refresh,client});
    this.hostSpeech=hostSpeech|| (on=>this.api('/rooms/'+this.room+'/host-speech',{client:this.client,speaking:on}));
    this.room=null;this.state=null;this.enabled=new Map();this.seen=new Set();this.epoch=0;
    this.stream=null;this.busy=false;this.ownTask=null;this.queue=[];this.playing=null;this.interrupting=Promise.resolve();
    this.$=id=>document.getElementById(id);
    this.$('realtime').onchange=()=>this.realtime().catch(e=>this.fail(e));
    this.$('record').onclick=()=>this.stream?this.finishManual():this.start(false).catch(e=>this.fail(e));
    this.$('stop-speaking').onclick=()=>this.stopSpeech('手动停止');
    this.$('media-target').onchange=()=>this.reset('切换交谈角色');
    this.$('voice-select').onchange=()=>this.stopSpeech('切换声音');
    this.$('voice-submit').onclick=()=>this.submit(this.$('voice-text').value,this.epoch).catch(e=>this.fail(e));
    globalThis.speechSynthesis?.addEventListener('voiceschanged',()=>this.voices());this.voices();
  }
  async configure(host){this.host=host;const c=await this.api('/media');this.$('media-controls').hidden=!host;
    this.available=c.asr_configured;this.$('media-status').textContent=c.asr_configured?'语音识别已配置；请点击开始录音':'平台未配置ASR；键盘聊天仍可使用';
    this.$('record').disabled=!c.asr_configured;this.$('realtime').disabled=!c.asr_configured;
  }
  voices(){const select=this.$('voice-select'),old=select.value;select.replaceChildren();
    for(const v of globalThis.speechSynthesis?.getVoices()||[]){if(!v.localService)continue;const o=document.createElement('option');o.value=v.voiceURI;o.textContent=v.name+' · '+v.lang;select.append(o);}
    if([...select.options].some(o=>o.value===old))select.value=old;
    if(!select.options.length){const o=document.createElement('option');o.value='';o.textContent='没有可用的本地朗读声音';select.append(o);}
  }
  log(feature,role,text='',detail='',enabled=false,room=this.room){
    if(!room||!role)return Promise.resolve();
    return this.api('/rooms/'+room+'/media-event',{role,client:this.client,feature,enabled,text:text.slice(0,3000),detail:detail.slice(0,300)})
      .catch(()=>this.note('语音状态日志未送达，网络恢复后请查看记录',true));
  }
  update(state){
    if(state.runtime?.enabled && state.runtime.state!=='RUNNING'){this.stopSpeech('整场暂停');this.queue=[];this.state=state;return;}
    if(this.state?.runtime?.branch && this.state.runtime.branch!==state.runtime?.branch){this.reset('时间回溯');this.seen=new Set(state.tasks.filter(t=>t.result).map(t=>t.id));}
    if(this.room!==state.id){this.reset('切换房间');this.room=state.id;this.enabled.clear();this.seen=new Set(state.tasks.filter(t=>t.result).map(t=>t.id));}
    this.state=state;
    const target=this.$('media-target'),old=target.value;
    if([...target.options].map(o=>o.value).join()!==state.roles.map(r=>r.id).join()){
      target.replaceChildren();for(const r of state.roles){const o=document.createElement('option');o.value=r.id;o.textContent=r.name;target.append(o);}if(state.roles.some(r=>r.id===old))target.value=old;
    }
    for(const task of [...state.tasks].reverse()){
      if(!task.result||this.seen.has(task.id))continue;this.seen.add(task.id);
      if(!task.result.failed&&this.enabled.get(task.role)&&['proposed','approved','discussed'].includes(task.status)&&task.version===state.version){
        this.queue.push({role:task.role,text:task.result.speech,task:task.id});
      }
    }
    // 现场变化或取消后，旧回答不继续播放。前端轮询最多约2秒延迟。
    if(this.playing){const t=state.tasks.find(t=>t.id===this.playing.task);if(state.talk?.host_speaking||!t||['cancelled','rejected','failed'].includes(t.status)||t.version!==state.version)this.stopSpeech(state.talk?.host_speaking?'主持人开始发言':'现场变化');}
    this.queue=this.queue.filter(q=>{const t=state.tasks.find(t=>t.id===q.task);return t&&['proposed','approved','discussed'].includes(t.status)&&t.version===state.version;});
    this.playNext();
  }
  roleControl(role){const label=document.createElement('label'),input=document.createElement('input');input.type='checkbox';input.checked=!!this.enabled.get(role.id);
    input.onchange=()=>{this.enabled.set(role.id,input.checked);this.log('语音开关',role.id,'','只朗读此浏览器中新到的角色发言',input.checked);if(!input.checked){this.queue=this.queue.filter(q=>q.role!==role.id);if(this.playing?.role===role.id)this.stopSpeech('关闭角色语音');}};
    label.append(input,document.createTextNode(' 朗读'+role.name+'（此浏览器）'));return label;
  }
  async playNext(){if(this.playing||!this.queue.length||this.state?.talk?.host_speaking)return;const q=this.queue.shift();if(!q.text?.trim())return this.playNext();
    const synth=globalThis.speechSynthesis,voice=synth?.getVoices().find(v=>v.localService&&v.voiceURI===this.$('voice-select').value);
    if(!voice){this.log('播放失败',q.role,q.text,'没有本地声音',true);this.note('没有可用的本地朗读声音；请安装系统声音或使用原VT语音',true);return;}
    const utterance=new SpeechSynthesisUtterance(q.text),generation=this.epoch;utterance.voice=voice;this.playing=q;q.room=this.room;
    try{await this.api('/rooms/'+q.room+'/audio-lease',{client:this.client,task:q.task,playing:true});}
    catch(e){if(this.playing===q)this.playing=null;this.log('播放失败',q.role,'','房间发言锁：'+e.message,true);return;}
    if(generation!==this.epoch||this.playing!==q){await this.release(q);return;}
    this.playTimer=setInterval(()=>this.api('/rooms/'+q.room+'/audio-lease',{client:this.client,task:q.task,playing:true}).catch(()=>this.stopSpeech('发言锁失效')),15000);
    utterance.onstart=()=>this.log('播放开始',q.role,q.text,'浏览器报告的播放开始',true);
    const finish=async(feature,detail='')=>{if(generation!==this.epoch||this.playing!==q)return;this.playing=null;await this.release(q);this.log(feature,q.role,q.text,detail,true);this.playNext();};
    utterance.onend=()=>finish('播放完成');utterance.onerror=e=>finish('播放失败',e.error);synth.speak(utterance);
  }
  async release(q){clearInterval(this.playTimer);this.playTimer=null;if(q)try{await this.api('/rooms/'+q.room+'/audio-lease',{client:this.client,task:q.task,playing:false});}catch(e){this.note('结束朗读回执未送达，租约会自动过期',true);}}
  stopSpeech(reason){const old=this.playing;this.playing=null;this.queue=[];globalThis.speechSynthesis?.cancel();if(old){this.release(old);this.log('播放停止',old.role,old.text,reason,true);}}
  reset(reason){const role=this.$('media-target').value,old=this.room;
    this.epoch++;this.busy=false;this.stopSpeech(reason);this.stopCapture();this.$('realtime').checked=false;this.$('voice-text').value='';
    if(this.voiceHolding){this.voiceHolding=false;this.hostSpeech(false).catch(e=>this.note(e.message,true));}
    if(old&&role)this.log('实时聊天开关',role,'',reason,false,old);this.ownTask=null;
  }
  fail(error){this.note(error.message,true);this.$('media-status').textContent=error.message;this.reset('语音失败，关闭收音');this.log('麦克风失败',this.$('media-target').value,'',error.name||'Error',false);}
  async realtime(){const role=this.$('media-target').value,on=this.$('realtime').checked;
    if(!on){this.reset('关闭实时聊天');return;}this.log('实时聊天开关',role,'','连续收音；发言结束后生成提案，仍需GM裁决',true);await this.start(true);}
  async start(continuous){
    if(this.stream)this.stopCapture();if(!globalThis.isSecureContext||!navigator.mediaDevices?.getUserMedia)throw Error('麦克风需要localhost或HTTPS，请允许此站点访问麦克风');
    if(!this.room||!this.$('media-target').value)throw Error('请先选择房间和交谈角色');
    const epoch=++this.epoch,stream=await navigator.mediaDevices.getUserMedia({audio:{echoCancellation:true,noiseSuppression:true,channelCount:1}});
    if(epoch!==this.epoch){stream.getTracks().forEach(t=>t.stop());return;}
    this.stream=stream;this.continuous=continuous;this.chunks=[];this.samples=0;this.talking=false;this.silence=0;
    const Ctx=globalThis.AudioContext||globalThis.webkitAudioContext;this.audio=new Ctx();await this.audio.resume();
    if(epoch!==this.epoch)return;
    this.source=this.audio.createMediaStreamSource(stream);this.processor=this.audio.createScriptProcessor(4096,1,1);
    this.source.connect(this.processor);this.processor.connect(this.audio.destination);
    this.processor.onaudioprocess=e=>{
      if(epoch!==this.epoch||this.busy)return;const pcm=e.inputBuffer.getChannelData(0),dt=pcm.length/this.audio.sampleRate;
      const loud=Math.sqrt(pcm.reduce((s,x)=>s+x*x,0)/pcm.length)>.018;
      if(!continuous||this.talking||loud){if(continuous&&!this.talking){this.talking=true;this.interrupting=this.bargeIn(epoch);}
        this.chunks.push(new Float32Array(pcm));this.samples+=pcm.length;this.silence=loud?0:this.silence+dt;
        if(this.samples/this.audio.sampleRate>=29||(continuous&&this.talking&&this.silence>=1))this.recognize(epoch,continuous).catch(e=>this.fail(e));}
    };
    this.$('record').textContent=continuous?'结束实时收音':'结束录音并识别';this.$('media-status').textContent=continuous?'正在听你说话；结束后只向所选角色提问':'正在录音；点击结束后可检查识别文字';
    this.log('麦克风启动',this.$('media-target').value,'',continuous?'连续收音':'手动录音',true);
    if(!continuous){this.voiceHolding=true;await this.hostSpeech(true);}
  }
  stopCapture(){this.stream?.getTracks().forEach(t=>t.stop());this.stream=null;if(this.processor){this.processor.onaudioprocess=null;this.processor.disconnect();this.source.disconnect();this.processor=null;}
    this.audio?.close().catch(()=>{});this.audio=null;this.$('record').textContent='开始录音';}
  async bargeIn(epoch){this.stopSpeech('用户开始说话');this.$('media-status').textContent='正在听；已停止本浏览器朗读';
    if(epoch===this.epoch){this.voiceHolding=true;await this.hostSpeech(true);}
    const task=this.ownTask;this.ownTask=null;if(task&&epoch===this.epoch){const pending=this.state.tasks.find(t=>t.id===task);if(pending&&['queued','running','proposed'].includes(pending.status)){
      try{await this.api('/rooms/'+this.room+'/tasks/'+task+'/cancel',{});}catch(e){this.note('旧提案取消失败：'+e.message,true);}
    }}
  }
  async finishManual(){if(this.continuous){this.reset('结束实时收音');return;}await this.recognize(this.epoch,false).catch(e=>this.fail(e));}
  async recognize(epoch,automatic){if(this.busy||!this.samples)return;this.busy=true;
    const audio=wavBase64(this.chunks,this.audio.sampleRate),role=this.$('media-target').value,room=this.room;
    this.chunks=[];this.samples=0;this.talking=false;this.silence=0;if(!automatic)this.stopCapture();this.$('media-status').textContent='正在识别；此时暂不采集下一句';
    try{const result=await this.api('/rooms/'+room+'/voice',{role,client:this.client,audio});if(epoch!==this.epoch||room!==this.room)return;
      this.$('voice-text').value=result.text;this.$('media-status').textContent=automatic?'识别完成，正在提交；稍后继续听':'请检查文字后点击发送';
      if(automatic)await this.submit(result.text,epoch);
    }catch(error){if(epoch===this.epoch)throw error;
    }finally{if(epoch===this.epoch){this.busy=false;if(this.voiceHolding){this.voiceHolding=false;await this.hostSpeech(false);}if(automatic)this.$('media-status').textContent='正在听；请等待角色提案或进行GM裁决';}}
  }
  async submit(text,epoch){text=text.trim();if(!text)throw Error('请先输入或识别一句话');const role=this.$('media-target').value;
    if(fastSignal(text)){await this.bargeIn(epoch);if(!this.stream&&this.voiceHolding){this.voiceHolding=false;await this.hostSpeech(false);}await this.log('快速信号',role,text,'规则触发，不调用模型；取消本浏览器待定提案',true);this.note('已停止朗读');return;}
    await this.interrupting;
    if(epoch!==this.epoch)return;await this.api('/rooms/'+this.room+'/dialogue',{text});const response=await this.api('/rooms/'+this.room+'/turn',{role,question:text});
    if(epoch!==this.epoch)return;this.ownTask=response.task_id;await this.log('输入提交',role,text,'完整输入；只生成行动提案',this.$('realtime').checked);this.note('已提交给所选角色，剧情仍需主持人裁决');await this.refresh(true);
  }
}
