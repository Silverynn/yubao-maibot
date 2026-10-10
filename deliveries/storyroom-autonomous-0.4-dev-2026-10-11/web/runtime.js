// Runtime controls are separate from the old per-round discussion buttons.
export class RoomRuntime {
  constructor({api,context,refresh,note,media}) {
    Object.assign(this,{api,context,refresh,note,media});this.$=id=>document.getElementById(id);this.timer=null;this.stream=null;this.streamRoom=null;
    const bind=(id,fn)=>this.$(id).onclick=async()=>{try{await fn();}catch(e){note(e.message,true);}};
    for(const [id,operation] of [['run-pause','pause'],['run-resume','resume'],['gm-takeover','takeover'],['gm-handoff','handoff'],['gm-release','release']])bind(id,()=>this.control(operation));
    bind('load-checkpoints',()=>this.loadTimeline());bind('rewind-preview',()=>this.preview());bind('rewind-confirm',()=>this.rewind());
    bind('export-play',()=>this.download());bind('archive-play',()=>this.download(this.$('branch-select').value));
    addEventListener('pagehide',()=>{clearInterval(this.timer);this.stream?.close();});
  }
  async control(operation){const {room,state}=this.context();await this.api('/rooms/'+room+'/runtime',{operation,revision:state.runtime.revision});if(['pause','takeover'].includes(operation))this.media.reset('运行控制：'+operation);await this.refresh(true);}
  update(state){
    const {actor}=this.context(),r=state.runtime;this.$('runtime-controls').hidden=!actor.host;
    const status=r.gm_status==='未配置'&&r.gm_configured?'已配置，等待交还AI':r.gm_status;
    const sceneStatus={unconfigured:'未配置',pending:'等待生成',generating:'正在生成',ready:'已生成',held:'暂停保留',failed:'生成失败'}[state.scene_image?.status]||state.scene_image?.status;
    this.$('run-status').textContent=`${r.enabled?r.state:'旧版讨论模式'} · 主持：${r.gm_mode==='AI'?'AI':'人类'} · AI状态：${status} · GM调用 ${r.gm_calls}/${r.gm_budget} · PC调用 ${r.pc_calls}/${r.pc_budget} · 背景 ${sceneStatus} · 分支 ${r.branch.slice(0,8)}`+(r.error?' · '+r.error:'');
    this.$('run-warning').textContent=state.rules.coverage;
    this.$('gm-handoff').disabled=!r.gm_configured;
    this.$('run-pause').disabled=r.enabled&&r.state==='PAUSED';this.$('rewind-confirm').disabled=!(r.enabled&&r.state==='PAUSED'&&this.previewId);
    if(this.timerRoom!==state.id){clearInterval(this.timer);this.timerRoom=state.id;this.timer=setInterval(async()=>{const {room,state,actor}=this.context();if(actor.host&&state?.runtime.gm_mode==='HUMAN'&&state.runtime.controller===actor.id&&state.runtime.controller_until>Date.now()/1000)try{await this.api('/rooms/'+room+'/controller/renew',{});}catch(e){this.note(e.message,true);}},25000);}
    if(this.streamRoom!==state.id){this.stream?.close();this.streamRoom=state.id;this.stream=new EventSource('/api/rooms/'+state.id+'/runtime-stream');this.stream.onmessage=e=>{const r=JSON.parse(e.data);if(r.enabled&&r.state!=='RUNNING'||this.lastBranch&&this.lastBranch!==r.branch||this.lastGM&&this.lastGM!==r.gm_epoch)this.media.reset('房间暂停、回溯或主持人切换');this.lastBranch=r.branch;this.lastGM=r.gm_epoch;this.refresh().catch(()=>{});};}
  }
  async loadTimeline(){const {room}=this.context();const t=await this.api('/rooms/'+room+'/timeline');this.$('checkpoint-select').replaceChildren();for(const c of t.checkpoints){const o=document.createElement('option');o.value=c.id;o.textContent=`${new Date(c.created*1000).toLocaleString()} · ${c.label} · ${c.branch.slice(0,8)} #${c.seq}`;this.$('checkpoint-select').append(o);}this.$('branch-select').replaceChildren();for(const b of t.branches){const o=document.createElement('option');o.value=b.id;o.textContent=(b.id===t.runtime.branch?'当前：':'归档：')+b.label+' '+b.id.slice(0,8);this.$('branch-select').append(o);}this.previewId=null;this.$('rewind-details').textContent='选择节点后预览；回溯前先暂停整场游戏。';}
  async preview(){const {room}=this.context();const id=this.$('checkpoint-select').value;if(!id)throw Error('请先加载并选择检查点');const p=await this.api('/rooms/'+room+'/checkpoints/'+id);this.previewId=id;this.$('rewind-details').textContent=JSON.stringify(p,null,2);await this.refresh(true);}
  async rewind(){const {room,state}=this.context();if(!this.previewId)throw Error('请先预览检查点');if(!confirm('将恢复预览中的状态，并在新分支继续。旧未来归档，API费用无法撤销。确认回溯？'))return;await this.api('/rooms/'+room+'/rewind',{checkpoint:this.previewId,revision:state.runtime.revision});this.media.reset('时间回溯');this.previewId=null;await this.refresh(true);await this.loadTimeline();this.note('已恢复，房间保持暂停。请接管或交还主持人，再恢复游戏。');}
  async download(branch){const {room}=this.context();const response=await fetch('/api/rooms/'+room+'/script.md'+(branch?'?branch='+encodeURIComponent(branch):''));if(!response.ok)throw Error('剧本读取失败');const url=URL.createObjectURL(await response.blob());const a=document.createElement('a');a.href=url;a.download='跑团剧本-'+(branch?.slice(0,8)||'当前分支')+'.md';a.click();URL.revokeObjectURL(url);}
}
