import {initStage,updateStage} from './stage.js';
import {RoomMedia} from './media.js';
import {RoomEditor} from './author.js';
const $=id=>document.getElementById(id);
let actor=null, room=null, state=null, catalog=[], fingerprint='', poll=null, polling=false;
const stageClient=crypto.randomUUID();
const media=new RoomMedia({api,note,refresh,client:stageClient,hostSpeech:on=>editor.micSpeech(on)});
const editor=new RoomEditor({api,note,refresh,context:()=>({actor,room,state}),client:stageClient});
const drafts=new Map();
const cardDrafts=new Map();
function cardEditor(role){
  const section=node('details');section.append(node('summary','编辑跑团角色卡（与QQ人格分开）'));
  const draft=cardDrafts.get(role.id)||{background:role.card.background||'',goal:role.card.goal||'',skills:{investigate:0,craft:0,social:0,...role.card.skills}};cardDrafts.set(role.id,draft);
  for(const [key,label] of [['background','角色过去'],['goal','在本剧本中的目标']]){const l=node('label',label),input=node('textarea');input.value=draft[key];input.maxLength=key==='background'?2000:500;input.oninput=()=>draft[key]=input.value;l.append(input);section.append(l);}
  for(const [key,label] of [['investigate','调查'],['craft','动手'],['social','交涉']]){const l=node('label',label+'加值（0～3）'),input=node('input');input.type='number';input.min=0;input.max=3;input.value=draft.skills[key];input.oninput=()=>draft.skills[key]=Number(input.value);l.append(input);section.append(l);}
  section.append(button('保存角色卡',async()=>{await api('/rooms/'+room+'/roles/'+role.id+'/card',draft);cardDrafts.delete(role.id);note('角色卡已保存；修改会使其他旧提案过时');await refresh(true);}));return section;
}
function showAudit(task){return async()=>{const data=await api('/rooms/'+room+'/tasks/'+task.id+'/audit');const blob=new Blob([JSON.stringify(data,null,2)],{type:'application/json'}),url=URL.createObjectURL(blob);const a=node('a');a.href=url;a.download='行动记录-'+task.id.slice(0,8)+'.json';a.click();URL.revokeObjectURL(url);};}
function checkControls(task,draft){
  const box=node('div');
  if(task.check){const c=task.check;box.append(node('p',`行动检定：${c.skill} · d20 + ${c.modifier} ≥ ${c.dc}`));if(c.roll===null)box.append(button('为此行动实际投骰一次',async()=>{await api('/rooms/'+room+'/tasks/'+task.id+'/roll',{});await refresh(true);}));else box.append(node('p',`实际点数 ${c.roll} + ${c.modifier} = ${c.total} · ${c.success?'成功':'失败'}（后果仍需裁决）`));}
  else{const skill=node('select');for(const [value,label] of [['investigate','调查'],['craft','动手'],['social','交涉']]){const o=node('option',label);o.value=value;skill.append(o);}skill.value=draft.skill||'investigate';skill.onchange=()=>draft.skill=skill.value;const dc=node('input');dc.type='number';dc.min=1;dc.max=23;dc.value=draft.dc||12;dc.oninput=()=>draft.dc=Number(dc.value);box.append(node('label','可选：指定此行动的检定（锁定后不能改难度）'),skill,dc,button('锁定检定',async()=>{await api('/rooms/'+room+'/tasks/'+task.id+'/check',{skill:skill.value,dc:Number(dc.value)});await refresh(true);}));}
  return box;
}
function note(text,error=false){$('notice').hidden=false;$('notice').textContent=text;$('notice').className=error?'error':'';setTimeout(()=>$('notice').hidden=true,6000);}
async function api(path,data){const response=await fetch('/api'+path,{method:data===undefined?'GET':'POST',headers:data===undefined?{}:{'Content-Type':'application/json'},body:data===undefined?undefined:JSON.stringify(data)});const result=await response.json();if(!response.ok)throw Error(typeof result.detail==='string'?result.detail:'参数未通过校验');return result;}
function wrap(fn){return async event=>{event?.preventDefault();try{await fn(event);}catch(e){note(e.message,true);}};}
function node(tag,text,cls){const e=document.createElement(tag);if(text!==undefined)e.textContent=text;if(cls)e.className=cls;return e;}
function button(text,handler){const b=node('button',text);b.onclick=wrap(handler);return b;}
function showKeys(items){$('keys').hidden=false;$('key-values').replaceChildren();for(const item of items){const row=node('label',item.name,'key-row');const input=node('input');input.value=item.token;input.readOnly=true;row.append(input);$('key-values').append(row);}}
async function enter(){actor=await api('/me');$('identity').textContent=(actor.host?'主持人 · ':'玩家 · ')+actor.name;$('login').hidden=true;$('workspace').hidden=false;$('logout').hidden=false;$('new-room').hidden=!actor.host||!!actor.room;$('dm-panel').hidden=!actor.host;$('host-invites').hidden=!actor.host||!!actor.room;catalog=await api('/models');for(const id of ['model-one','model-two']){$(id).replaceChildren();for(const model of catalog){const option=node('option',model.name);option.value=model.name;$(id).append(option);}if(!catalog.length){const o=node('option','无Live2D（仍可跑团）');o.value='none';$(id).append(o);}}if(catalog.some(c=>c.name==='moonbell-20261003'))$('model-one').value='moonbell-20261003';if(catalog.some(c=>c.name==='ds-whale-girl'))$('model-two').value='ds-whale-girl';if(actor.host)await editor.loadTemplates();await media.configure(actor.host);await initStage(catalog);await listRooms();if(poll)clearInterval(poll);poll=setInterval(()=>refresh().catch(e=>note(e.message,true)),2000);}
async function listRooms(preferred){const list=await api('/rooms');$('rooms').replaceChildren();for(const item of list){const o=node('option',item.name);o.value=item.id;$('rooms').append(o);}room=preferred||list[0]?.id||null;if(room){$('rooms').value=room;await refresh(true);}else{$('creation').hidden=!actor.host;$('table').hidden=true;}}
async function refresh(force=false){if(!room||polling)return;polling=true;try{state=await api('/rooms/'+room);const stableRoles=state.roles.map(({last_seen,...r})=>r);const next=JSON.stringify([state.node,state.flags,stableRoles,state.tasks,state.events,state.experiences,state.talk,state.room_memory]);$('table').hidden=false;media.update(state);await updateStage(state,async(role,expression,label,status)=>{await api('/rooms/'+room+'/visual-receipt',{role,revision:state.roles.find(r=>r.id===role).visual.revision,client:stageClient,expression:String(expression),label,status});});if(next!==fingerprint||force){fingerprint=next;render();}}finally{polling=false;}}
function render(){
  editor.update(state);$('memory-editor').hidden=!actor.host;
  $('cohosts').replaceChildren();for(const h of state.hosts||[]){const item=node('div',h.name);if(actor.host&&!actor.room)item.append(button('撤销权限',async()=>{if(!confirm('撤销此主持人的房间权限？'))return;await api('/rooms/'+room+'/hosts/'+h.id+'/revoke',{});await refresh(true);}));$('cohosts').append(item);}
  $('story-title').textContent=state.story_title;$('stage-title').textContent=state.stage.title;$('stage-description').textContent=state.stage.description;$('world').textContent=state.world;$('outlook').textContent='本阶段的角色看法：'+state.stage.outlook;
  $('flags').replaceChildren(...state.flags.map(f=>node('span',flagName(f),'chip')));
  $('characters').replaceChildren();$('turn-buttons').replaceChildren();
  for(const role of state.roles){const card=node('div',undefined,'character');card.append(node('span',role.connected?'PC连接在线':'等待PC连接','badge '+(role.connected?'online':'')),node('strong',role.name),node('p','跑团心情：'+role.mood+'/100 · 展示情绪：'+role.visual.emotion+' · 合作信任：'+role.trust),node('p',role.avatar));
    card.append(media.roleControl(role));if(actor.host)card.append(cardEditor(role));
    if(actor.host)card.append(button('重新生成连接密钥',async()=>{if(!confirm('旧玩家密钥将失效，是否重新生成？'))return;const r=await api('/rooms/'+room+'/roles/'+role.id+'/rotate',{});showKeys([{name:role.name,token:r.token}]);}));
    $('characters').append(card);$('turn-buttons').append(button('请'+role.name+'行动',async()=>{await api('/rooms/'+room+'/turn',{role:role.id,question:$('turn-question').value});note('已邀请玩家；模型尚未返回时显示处理中');await refresh(true);}));}
  $('next-stage').replaceChildren();for(const edge of state.stage.next){const o=node('option',edge.label+(edge.requires?.length?'（需 '+edge.requires.map(flagName).join('、')+'）':''));o.value=edge.id;$('next-stage').append(o);}$('advance').disabled=!state.stage.next.length;
  $('experiences').replaceChildren();for(const memory of state.experiences){const who=state.roles.find(r=>r.id===memory.role)?.name||'角色',box=node('div',undefined,'event');box.append(node('strong',who+(memory.active?' · 可回忆':' · 已撤销')),node('p',memory.text));if(actor.host&&memory.active)box.append(button('撤销此经历的回忆',async()=>{if(!confirm('撤销后模型不会再回忆这条经历；不会自动回滚世界事实。继续吗？'))return;await api('/rooms/'+room+'/experiences/'+memory.id,{text:memory.text,active:false});await refresh(true);}));$('experiences').append(box);}
  $('tasks').replaceChildren();if(!state.tasks.length)$('tasks').append(node('p','主持人点名后，玩家提案会出现在这里。','hint'));
  for(const task of state.tasks){
    const box=node('div',undefined,'task'),name=state.roles.find(r=>r.id===task.role)?.name||'玩家';
    const statuses={queued:'轮流排队 / 等待连接或发言结束',running:'正在自行思考',proposed:task.result?.intent==='question'?'等待主持人回答':'等待主持人裁决',discussed:'讨论发言（没有执行行动）',answered:'主持人已回答',failed:'模型调用失败，未产生有效行动',approved:'已确认实际后果与经历',rejected:'已退回',cancelled:'现场变化，已取消'};
    box.append(node('strong',name),node('p',statuses[task.status]||task.status,'task-status'),node('p','主持人问题：'+task.question));
    if(task.held)box.append(node('p','主持人正在发言；本次输出暂存，结束后再展示。','hint'));
    if(task.version!==state.version)box.append(node('p','现场已经变化：请退回此旧提案，再邀请玩家根据新现场行动。','hint'));
    if(task.result){box.append(node('p','发言：'+task.result.speech),node('p','行动提案：'+task.result.action),node('p','展示情绪：'+task.result.emotion));}
    if(actor.host)box.append(button('下载本次行动记录',showAudit(task)));
    if(actor.host){if(task.status==='proposed'&&task.result?.intent==='question'){
      const draft=drafts.get(task.id)||{};drafts.set(task.id,draft);
      const answer=node('textarea');answer.placeholder='回答玩家的问题，说明已经确认的现场信息';answer.value=draft.answer||'';answer.oninput=()=>draft.answer=answer.value;
      box.append(answer,button('回复并继续讨论',async()=>{await api('/rooms/'+room+'/tasks/'+task.id+'/answer',{text:answer.value});await refresh(true);}));
    }else if(task.status==='proposed'&&!task.held){
      const draft=drafts.get(task.id)||{ruling:'',flags:[],trust:0};drafts.set(task.id,draft);
      box.append(checkControls(task,draft));
      const mood=node('input');mood.type='number';mood.min=-10;mood.max=10;mood.value=draft.mood||0;mood.oninput=()=>draft.mood=Number(mood.value);const moodLabel=node('label','实际后果引起的心情变化（−10～+10）');moodLabel.append(mood);box.append(moodLabel);
      const ruling=node('textarea');ruling.placeholder='写下裁决：此行动是否成功，实际发生了什么？';ruling.setAttribute('aria-label',name+'的裁决');ruling.value=draft.ruling;ruling.oninput=()=>draft.ruling=ruling.value;
      const flags=node('select');flags.multiple=true;flags.size=3;flags.setAttribute('aria-label',name+'的已确认标记');for(const f of state.allowed_flags){const o=node('option',flagName(f));o.value=f;o.selected=draft.flags.includes(f);flags.append(o);}flags.onchange=()=>draft.flags=[...flags.selectedOptions].map(o=>o.value);
      const trust=node('select');trust.setAttribute('aria-label',name+'的合作信任变化');for(const value of [0,1,-1,2,-2]){const o=node('option','合作信任 '+(value>=0?'+':'')+value);o.value=value;trust.append(o);}trust.value=draft.trust;trust.onchange=()=>draft.trust=Number(trust.value);
      box.append(ruling,node('label','确认实际后果（检定失败也可确认失败经历）；事实标记可不选：'),flags,trust,button('确认裁决与经历',async()=>{await api('/rooms/'+room+'/tasks/'+task.id+'/decide',{approve:true,ruling:ruling.value,flags:[...flags.selectedOptions].map(o=>o.value),trust_delta:Number(trust.value),mood_delta:Number(mood.value)});note('裁决已写入世界线');await refresh(true);}));
    }if(['queued','running','proposed','failed'].includes(task.status))box.append(button('退回 / 取消',async()=>{await api('/rooms/'+room+'/tasks/'+task.id+'/cancel',{});await refresh(true);}));}
    $('tasks').append(box);}
  $('timeline').replaceChildren();$('event-count').textContent='最近 '+state.events.length+' 条';for(const item of state.events){const e=node('article',undefined,'event');e.append(node('time',item.at.replace('T',' ').slice(0,19)),node('h3',item.kind+' · '+item.actor),node('p',Object.entries(item.detail).map(([k,v])=>k+'：'+(v!==null&&typeof v==='object'?JSON.stringify(v,null,2):v??'未设置')).join('\n')));$('timeline').append(e);}
}
function flagName(flag){return {gear_found:'找到了备用齿轮',lens_cleaned:'清理了镜片',boat_warned:'已向渔船预警'}[flag]||flag;}
$('login-form').onsubmit=wrap(async()=>{await api('/login',{token:$('login-key').value});$('login-key').value='';await enter();});
$('logout').onclick=wrap(async()=>{await editor.releaseAll();media.reset('退出');await api('/logout',{});location.reload();});
$('new-room').onclick=()=>{$('creation').hidden=!$('creation').hidden;};
$('hide-keys').onclick=()=>{$('keys').hidden=true;$('key-values').replaceChildren();};
$('create-form').onsubmit=wrap(async()=>{const r=await api('/rooms',{name:$('room-name').value,names:[$('pc-one').value,$('pc-two').value],avatars:[$('model-one').value,$('model-two').value],story:$('story-select').value});showKeys(r.connections);$('creation').hidden=true;await listRooms(r.room_id);});
$('rooms').onchange=wrap(async()=>{await editor.releaseAll();media.reset('切换房间');room=$('rooms').value;fingerprint='';await refresh(true);});
$('narrate-form').onsubmit=wrap(async()=>{await api('/rooms/'+room+'/narrate',{text:$('narration').value});$('narration').value='';await refresh(true);});
$('roll').onclick=wrap(async()=>{const r=await api('/rooms/'+room+'/dice',{expression:$('dice-expression').value,dc:$('dice-dc').value?Number($('dice-dc').value):null});$('dice-result').textContent='点数 '+r['每颗点数'].join(' + ')+' → '+r['总点数']+'，'+r['检定'];await refresh(true);});
$('advance').onclick=wrap(async()=>{await api('/rooms/'+room+'/advance',{target:$('next-stage').value});await refresh(true);});
$('demo').onclick=wrap(async()=>{await api('/rooms/'+room+'/demo',{});note('两位演示玩家已提交提案，没有调用模型');await refresh(true);});
$('invite-host').onclick=wrap(async()=>{if(!room)throw Error('请先创建房间');const r=await api('/rooms/'+room+'/hosts',{name:$('cohost-name').value});showKeys([r]);await refresh(true);});
api('/me').then(enter).catch(()=>{});
