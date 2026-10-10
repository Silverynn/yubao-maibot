// 常用字段用表单编辑；JSON可导入、导出和交给伙伴AI编辑。服务端做最终校验。
export class RoomEditor {
  constructor({api,note,refresh,context,client}) {
    Object.assign(this,{api,note,refresh,context,client});this.$=id=>document.getElementById(id);
    this.script=null;this.hash='';this.templateHash='';this.heldRoom=null;this.timer=null;
    const bind=(id,fn)=>this.$(id).onclick=async()=>{try{await fn();}catch(e){note(e.message,true);}};
    bind('edit-story',async()=>{this.$('story-editor').hidden=!this.$('story-editor').hidden;await this.loadTemplates();});
    bind('load-template',()=>this.loadTemplate());bind('load-room-script',()=>this.loadRoom());
    bind('script-json-read',()=>{this.script=JSON.parse(this.$('script-json').value);this.renderScript();});
    bind('script-export',()=>this.download());
    this.$('script-import').onchange=async e=>{try{const f=e.target.files[0];if(!f)return;if(f.size>100000)throw Error('文件超过100KB');this.script=JSON.parse(await f.text());this.hash='';this.templateHash='';this.renderScript();}catch(error){note(error.message,true);}};
    bind('script-save',async()=>{this.readGeneral();const found=this.templates.find(t=>t.id===this.script.id);const r=await api('/stories/save',{script:this.script,expected_hash:found?.hash||''});this.templateHash=r.hash;await this.loadTemplates();note('模板已保存；正在玩的房间未改变');});
    bind('script-apply',async()=>{if(!this.hash)throw Error('请先载入当前房间剧本，再修改并应用');this.readGeneral();const {room}=context();await api('/rooms/'+room+'/script',{script:this.script,expected_hash:this.hash});await this.loadRoom();await refresh(true);note('已显式应用，当前阶段和记忆保留；讨论已暂停');});
    bind('add-stage',()=>{this.readGeneral();const key=this.$('new-stage-id').value.trim();if(!/^[\p{L}\p{N}_-]{1,50}$/u.test(key))throw Error('阶段ID需1～50个文字、数字、下划线或短横线');if(this.script.nodes[key])throw Error('阶段ID已存在');this.script.nodes[key]={title:key,scene:'harbor',description:'请填写此阶段的现场',outlook:'',next:[],ending:true};this.renderScript();});
    this.$('stage-picker').onchange=()=>this.stageForm();
    bind('stage-save',()=>this.saveStage());
    bind('stage-delete',()=>{const key=this.$('stage-picker').value;if(key===this.script.start)throw Error('不能删除当前起点，请先更改起点');delete this.script.nodes[key];this.renderScript();note('阶段已删除；还需修改指向它的分支，保存时会校验');});
    bind('talk-open',async()=>{const {room}=context();if(this.heldRoom)await this.hold(false);await api('/rooms/'+room+'/talk',{opened:true,limit:Number(this.$('talk-limit').value)});await refresh(true);});
    bind('talk-pause',async()=>{const {room}=context();await api('/rooms/'+room+'/talk',{opened:false,limit:Number(this.$('talk-limit').value)});await refresh(true);note('停止后续自主讨论；已开始思考的任务请按需取消');});
    bind('host-hold',()=>this.hold(!this.manualHold));
    bind('memory-add',async()=>{const {room}=context();await api('/rooms/'+room+'/memory',{subject:this.$('memory-subject').value,text:this.$('memory-text').value});this.$('memory-text').value='';await refresh(true);});
    bind('participant-add',async()=>{const {room}=context();await api('/rooms/'+room+'/participants',{name:this.$('participant-name').value,profile:this.$('participant-profile').value});await refresh(true);});
    bind('dialogue-send',async()=>{const {room}=context();await api('/rooms/'+room+'/dialogue',{text:this.$('dialogue-text').value});this.$('dialogue-text').value='';await refresh(true);});
  }
  async loadTemplates(){const {actor}=this.context();this.$('script-save').hidden=!!actor.room;this.$('load-template').hidden=!!actor.room;
    this.templates=actor.room?[]:await this.api('/stories');for(const id of ['story-select','template-picker']){const old=this.$(id).value;this.$(id).replaceChildren();for(const t of this.templates){const o=document.createElement('option');o.value=t.id;o.textContent=t.script.title;this.$(id).append(o);}if(this.templates.some(t=>t.id===old))this.$(id).value=old;}
  }
  async loadTemplate(){const t=this.templates.find(t=>t.id===this.$('template-picker').value);if(!t)throw Error('请先选择模板');this.script=structuredClone(t.script);this.hash='';this.templateHash=t.hash;this.renderScript();}
  async loadRoom(){const {room}=this.context();if(!room)throw Error('请先选择房间');const r=await this.api('/rooms/'+room+'/script');this.script=r.script;this.hash=r.hash;this.renderScript();}
  renderScript(){for(const [id,key] of [['script-id','id'],['script-title','title'],['script-world','world'],['script-start','start']])this.$(id).value=this.script[key]||'';
    this.$('script-flags').value=(this.script.flags||[]).join(',');this.$('stage-picker').replaceChildren();for(const [key,node] of Object.entries(this.script.nodes||{})){const o=document.createElement('option');o.value=key;o.textContent=key+' · '+node.title;this.$('stage-picker').append(o);}this.stageForm();this.json();
  }
  readGeneral(){if(!this.script)throw Error('请先载入剧本');for(const [id,key] of [['script-id','id'],['script-title','title'],['script-world','world'],['script-start','start']])this.script[key]=this.$(id).value.trim();this.script.flags=this.$('script-flags').value.split(/[,，\n]/).map(s=>s.trim()).filter(Boolean);this.json();}
  stageForm(){const s=this.script.nodes[this.$('stage-picker').value];if(!s)return;for(const [id,key] of [['node-title','title'],['node-description','description'],['node-outlook','outlook'],['node-scene','scene']])this.$(id).value=s[key]||'';this.$('node-ending').checked=!!s.ending;this.$('node-branches').value=(s.next||[]).map(e=>[e.id,e.label,(e.requires||[]).join(',')].join(' | ')).join('\n');}
  saveStage(){this.readGeneral();const s=this.script.nodes[this.$('stage-picker').value];for(const [id,key] of [['node-title','title'],['node-description','description'],['node-outlook','outlook'],['node-scene','scene']])s[key]=this.$(id).value.trim();s.ending=this.$('node-ending').checked;s.next=this.$('node-branches').value.split('\n').filter(l=>l.trim()).map(l=>{const [id,label,flags='']=l.split('|').map(s=>s.trim());if(!id||!label)throw Error('分支每行格式：目标ID | 显示名称 | 所需标记（可空）');return {id,label,requires:flags.split(',').map(s=>s.trim()).filter(Boolean)};});this.json();this.note('阶段已写入草稿，尚未保存模板或应用房间');}
  json(){this.$('script-json').value=JSON.stringify(this.script,null,2);}
  download(){this.readGeneral();const u=URL.createObjectURL(new Blob([JSON.stringify(this.script,null,2)],{type:'application/json'}));const a=document.createElement('a');a.href=u;a.download='剧本-'+this.script.id+'.json';a.click();URL.revokeObjectURL(u);}
  async hold(on){this.manualHold=on;await this.syncHold();}
  async micSpeech(on){this.micHold=on;await this.syncHold();}
  async releaseAll(){if(!this.heldRoom)return;this.manualHold=false;this.micHold=false;await this.syncHold();}
  async syncHold(){const {room}=this.context(),target=this.heldRoom||room,on=!!(this.manualHold||this.micHold);if(!target)throw Error('请先选择房间');await this.api('/rooms/'+target+'/host-speech',{client:this.client,speaking:on});clearInterval(this.timer);this.heldRoom=on?target:null;this.$('host-hold').textContent=this.manualHold?'我讲完了（释放发言）':'主持人开始讲话';if(on)this.timer=setInterval(()=>this.api('/rooms/'+target+'/host-speech',{client:this.client,speaking:true}).catch(e=>this.note('发言锁续期失败：'+e.message,true)),25000);await this.refresh(true);}
  update(state){const {actor}=this.context();this.$('room-settings').hidden=!actor.host;this.$('edit-story').hidden=!actor.host;
    const talk=state.talk||{};this.$('talk-status').textContent=talk.host_speaking?'主持人正在讲话；机器人先听':talk.opened&&talk.remaining>0?'允许自主讨论，本轮剩余 '+talk.remaining+' 次；同一时间只有一位发言':talk.opened?'本轮次数已用完，等待主持人继续讨论':'等待主持人介绍、回应或开放讨论';
    this.$('memory-list').replaceChildren();for(const n of state.room_memory?.notes||[])this.item('memory-list',n.subject,n.text,async(text,active)=>this.api('/rooms/'+state.id+'/memory',{...n,text,active}));
    this.$('participants-list').replaceChildren();for(const p of state.room_memory?.other_players||[])this.item('participants-list',p.name,p.profile,async(profile,active)=>this.api('/rooms/'+state.id+'/participants',{...p,profile,active}));
    this.$('memory-namespace').textContent='记忆空间：'+(state.room_memory?.namespace||'');
  }
  item(target,title,text,save){const {actor}=this.context(),box=document.createElement('div');box.className='event';const h=document.createElement('strong');h.textContent=title;const p=document.createElement('p');p.textContent=text;box.append(h,p);if(actor.host)for(const [name,active] of [['修改',true],['撤销',false]]){const b=document.createElement('button');b.textContent=name;b.onclick=async()=>{try{const next=active?prompt('修改内容（只影响本房间的后续回忆）',text):text;if(next===null)return;await save(next,active);await this.refresh(true);}catch(e){this.note(e.message,true);}};box.append(b);}this.$(target).append(box);}
}
