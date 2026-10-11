"""实时对话协调器。输入、只读任务、输出各自运行，用世代号拒绝迟到结果。"""
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, Optional

import asyncio
import hashlib
import json
import math
import re
import time
import uuid

from .speculation import eligible, evidence_key, normalize_input


FEATURES = {
 'bind': '会话绑定', 'listen': '流式听取', 'prefetch': '边听边查',
 'fast': '短句快反应', 'barge': '插话与取消', 'clarify': '适时澄清',
 'correct': '证据更正', 'final': '正式输入', 'output': '输出与播放账本',
 'qq': 'QQ多人隔离', 'failure': '故障隔离',
 'speculate': '提前生成', 'mode': '插件开关',
 'tool': '只读工具调用',
}


@dataclass
class Clip:
    clip_id: str
    epoch: int
    draft_id: str
    text: str
    group: str
    ordinal: int
    state: str = 'queued'
    position: float = 0.0
    requires_audio: bool = True
    commit_pending: bool = False


class Conversation:
    def __init__(self, session: str, visitor: str, connection: str, call, emit, audit):
        self.session, self.visitor, self.connection = session, visitor, connection
        self.call, self.emit, self.audit = call, emit, audit
        self.epoch = 0
        self.utterance = ''
        self.revision = 0
        self.partial = ''
        self.listening = False
        self.started = 0.0
        self.task: Optional[asyncio.Task] = None
        self.watch: Optional[asyncio.Task] = None
        self.query = ''
        self.result: Optional[Dict[str, Any]] = None
        self.clips: Dict[str, Clip] = {}
        self.finals: Dict[str, str] = {}
        self.native_ids: Dict[str, str] = {}
        self.voice = True
        self.closed = False
        self.last_clarify = 0.0
        self.last_fast = ''
        self.character_names = []
        self.pending_correction: Optional[Dict[str, Any]] = None
        self.correction_packet: Optional[Dict[str, Any]] = None
        self._workers = asyncio.Semaphore(2)
        self._cancel_lock = asyncio.Lock()
        self._tasks = set()
        self.query_ticket = 0
        self.preemptive = False
        self.spec_timer = None
        self.spec_task = None
        self.spec_attempts = 0
        self.spec_text = ''
        self.audio_input = False
        self.final_text = ''
        self.query_wait_seconds = 1.0
        self.query_timeout_seconds = 4.0
        self.stable_seconds = .65
        self.correction_count = 0
        self.deferred_result = None

    def log(self, feature: str, phase: str, scope=None, **data):
        identity=dict(plugin_loaded=True, plugin_enabled=not self.closed,
            session_enabled=not self.closed, preemptive_enabled=self.preemptive,
            visitor_id=self.visitor,connection_id=self.connection,
            utterance_id=self.utterance,epoch=self.epoch,revision=self.revision,
            message_id=self.native_ids.get(self.utterance,''))
        identity.update(listening=self.listening, audio_input=self.audio_input,
            since_input_ms=round((time.monotonic()-self.started)*1000, 1) if self.started else None)
        identity.update(scope or {})
        self.audit(FEATURES[feature],self.session,phase=phase,**identity,**data)

    async def host(self, action: str, **kwargs):
        result = await self.call('heart.realtime', action=action, session_id=self.session,
            visitor_id=self.visitor, connection_id=self.connection, **kwargs)
        if not isinstance(result, dict) or result.get('success') is False:
            raise RuntimeError(str(result))
        return result

    def spawn(self, coro):
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        def done(t):
            if not t.cancelled() and t.exception():
                self.log('failure', 'failed', reason=type(t.exception()).__name__)
        task.add_done_callback(done)
        return task

    async def set_mode(self, enabled: bool):
        if type(enabled) is not bool:
            raise ValueError('提前生成开关必须为布尔值')
        self.preemptive = enabled
        await self.host('mode', preemptive=enabled)
        if not enabled:
            for task in (self.spec_timer, self.spec_task):
                if task and not task.done():
                    task.cancel()
        self.log('mode', 'changed', output='提前生成已开启' if enabled else '提前生成已关闭')

    async def begin(self, utterance: str, voice: bool = True, audio: bool = False):
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', utterance):
            raise ValueError('发言编号无效')
        if utterance == self.utterance or utterance in self.finals:
            return {'duplicate': True, 'epoch': self.epoch,'utterance_id':utterance}
        await self.stop('用户开始说话')
        self.utterance, self.revision, self.partial = utterance, 0, ''
        self.listening, self.started = True, time.monotonic()
        self.voice, self.last_fast = type(voice) is bool and voice, ''
        self.query, self.result, self.pending_correction = '', None, None
        self.audio_input, self.final_text = audio is True, ''
        self.spec_attempts, self.spec_text, self.correction_count = 0, '', 0
        self.deferred_result = None
        self.log('listen', 'started', output='正在听，同时处理只读任务')
        await self.emit({'type':'heart-rt-state','epoch':self.epoch,'state':'listening',
                         'text':'正在听…可以继续说，也可以打断我。'})
        return {'epoch': self.epoch, 'utterance_id': utterance}

    async def stop(self, reason: str):
        async with self._cancel_lock:
            self.epoch += 1
            self.listening = False
            self.query = ''
            for task in (self.task, self.watch, self.spec_timer, self.spec_task):
                if task is not None and task is not asyncio.current_task() and not task.done():
                    task.cancel()
            for clip in self.clips.values():
                if clip.state in {'queued','playing'}:
                    clip.state = 'stopped'
            # 浏览器先暂停本地声音；服务器再取消模型/TTS，不能等待网络再停声。
            await self.emit({'type':'heart-rt-stop','epoch':self.epoch,'reason':reason})
            result = await self.host('begin', epoch=self.epoch)
            self.log('barge', 'completed', reason=reason, result=result,
                     output='旧播放与迟到结果被冻结')
            if result.get('local_cancelled') is False:
                raise RuntimeError('旧推理尚未确认结束；暂不接受新输出')

    async def input_end(self, utterance: str):
        """原生VAD已结束这一段录音；还没识别完不等于用户还在讲话。"""
        if utterance != self.utterance:
            raise ValueError('旧发言不能结束当前录音')
        if not self.listening:
            return {'duplicate': True}
        self.listening = False
        if self.spec_attempts == 0 and self.spec_timer and not self.spec_timer.done():
            self.spec_timer.cancel()
        self.log('listen', 'ended', output='录音已结束；等待整段转写，已开始的查询/草稿仍可完成')
        return {'accepted': True}

    @staticmethod
    def intent(text: str) -> str:
        # 只读意图才提前执行；记住/删除/提醒/发送不走 partial 工具路径。
        if re.search(r'(?:不要|别|不用|取消).{0,4}(?:查|找|记)|(?:记住|忘记|删除|发给|提醒)', text):
            return ''
        match = re.search(r'(?:查一下|查查|查询(?:一下)?|核实(?:一下)?|找一下|还记得|记不记得|你记得|帮我查(?:一下)?|帮我找(?:一下)?)(.{0,80})', text)
        if not match:
            return ''
        query = match.group(1).strip('，。！？、 ')
        # 流式ASR通常没有标点；已明确的查询后追加“我还想……”不应逐字重查。
        query = re.split(r'[，。！？!?；;\n]|(?:我还|另外|顺便)(?:想|要|需要)?', query, maxsplit=1)[0].strip()
        return query if len(query) >= 2 and query != '一下' else ''

    async def partial_input(self, utterance: str, revision: int, text: str):
        if utterance != self.utterance or not self.listening or utterance in self.finals:
            self.log('listen','discarded',reason='旧发言或已完成的临时转写')
            return
        if isinstance(revision,bool) or not isinstance(revision,int) or revision <= self.revision:
            return
        text = str(text)[:1000]
        text_changed = text != self.partial
        self.revision, self.partial = revision, text
        if self.spec_text and normalize_input(text) != normalize_input(self.spec_text):
            await self.host('spec_cancel', reason='用户继续补充或改口，旧草稿不复用')
            if self.spec_task and not self.spec_task.done():
                self.spec_task.cancel()
            self.log('speculate', 'discarded', reason='临时输入改变', input=text)
            self.spec_text = ''
        self.log('listen','partial',text=text, temporary=True,
                 elapsed_ms=(time.monotonic()-self.started)*1000)
        await self.emit({'type':'heart-rt-partial','epoch':self.epoch,'text':text})
        compact = re.sub(r'[\s，。！？!?,.]','',text)
        if compact in {'停','停止','停一下','别说了','等一下','等等'} and self.last_fast != compact:
            self.last_fast = compact
            self.log('fast','completed',signal=compact,output='已停下，不调用语言模型')
            await self.stop('明确停止短句')
            self.listening = True
            return
        name_signals={re.sub(r'[\s，。！？!?,.]','',name).casefold()+suffix
                      for name in self.character_names for suffix in ('','在吗')}
        if compact.casefold() in name_signals | {'你在吗'} and self.last_fast != compact:
            self.last_fast = compact
            self.log('fast','completed',signal=compact,output='我在，正在听。')
            await self.emit({'type':'heart-rt-ack','epoch':self.epoch,'text':'我在，正在听。'})
        query = self.intent(text)
        if query != self.query:
            self.query = query
            self.query_ticket += 1
            self.result = None
            if self.task and not self.task.done():
                self.task.cancel()
                self.log('prefetch','cancel_requested',reason='用户修改了检索目标')
            if query:
                self.task = self.spawn(self.prefetch(self.epoch, utterance, query, self.query_ticket))
        if text_changed:
            self.schedule_speculation()

    def schedule_speculation(self):
        if self.spec_attempts==0 and self.spec_timer and not self.spec_timer.done():
            self.spec_timer.cancel()
        if self.preemptive and self.audio_input and self.listening and self.spec_attempts == 0 and eligible(self.partial):
            self.spec_timer = self.spawn(self.speculate_after_pause(self.epoch, self.utterance, self.partial))

    async def speculate_after_pause(self, epoch: int, utterance: str, snapshot: str):
        await asyncio.sleep(self.stable_seconds)
        if (not self.preemptive or not self.listening or self.closed or epoch != self.epoch
                or utterance != self.utterance or snapshot != self.partial or self.spec_attempts):
            return
        if time.monotonic()-self.started > 20:
            self.log('speculate', 'skipped', reason='发言超过20秒，避免猜测长输入')
            return
        if self.query and self.result is None:
            self.log('speculate', 'skipped', reason='答案依赖的查询尚未完成')
            return
        self.spec_attempts += 1
        self.spec_text = self.partial
        revision = self.revision
        self.log('speculate', 'started', input=self.spec_text, temporary=True, attempt=self.spec_attempts)
        await self.emit({'type':'heart-rt-state','epoch':epoch,'state':'preparing',
                         'text':'正在提前准备草稿；你可以继续说，草稿暂不播放。'})
        self.spec_task = self.spawn(self.host('speculate',epoch=epoch,utterance_id=utterance,
            revision=revision,text=self.spec_text,evidence=self.result,evidence_key=evidence_key(self.result)))
        try:
            result = await self.spec_task
            if epoch==self.epoch and utterance==self.utterance and self.spec_text:
                self.log('speculate','ready', output=result.get('output',''), result=result, hidden=True)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.log('speculate','failed', reason=type(error).__name__, output='正式消息继续由原链路处理')

    async def read(self, query: str, purpose: str = '提前查询'):
        # 捕获调用所属输入，取消后的结果不能归到下一轮发言。
        scope = dict(epoch=self.epoch, utterance_id=self.utterance, revision=self.revision,
                     message_id=self.native_ids.get(self.utterance, ''), listening=self.listening)
        input_started = self.started
        async def observed(tool, invoke):
            call_id = uuid.uuid4().hex
            started = time.monotonic()
            fields = dict(scope=scope, tool=tool, call_id=call_id, purpose=purpose, input=query,
                          trigger='实时插件规则触发的只读查询；不是模型自行选择工具')
            def update_scope():
                fields['scope'] = dict(scope,
                    listening=self.listening if self.utterance == scope['utterance_id'] else False,
                    since_input_ms=round((time.monotonic()-input_started)*1000, 1) if input_started else None)
            update_scope()
            self.log('tool', 'started', **fields)
            try:
                output = await invoke()
                update_scope()
                self.log('tool', 'completed', **fields, output=output,
                         duration_ms=round((time.monotonic()-started)*1000, 1))
                return output
            except asyncio.CancelledError:
                update_scope()
                self.log('tool', 'cancelled', **fields, reason='调用被取消或外层查询截止；远端取消不保证成功')
                raise
            except Exception as error:
                update_scope()
                self.log('tool', 'timeout' if isinstance(error, asyncio.TimeoutError) else 'failed',
                         **fields, reason=type(error).__name__,
                         duration_ms=round((time.monotonic()-started)*1000, 1))
                raise
        async def all_sources():
            async with self._workers:
                native = await observed('heart.realtime.read（原生本人记忆）',
                                        lambda: self.host('read', query=query))
                # 插件只读取管理员指定的同一个事实表；不接受浏览器传入文件路径。
                local = await observed('local.facts（管理员本地事实表）',
                                       lambda: self.call('local.facts', query=query))
                return {'native': native, 'local': local}
        body = await asyncio.wait_for(all_sources(), timeout=self.query_timeout_seconds)
        body['version'] = hashlib.sha256(json.dumps(body,ensure_ascii=False,
            sort_keys=True).encode()).hexdigest()
        return body

    async def prefetch(self, epoch: int, utterance: str, query: str, ticket: int):
        revision=self.revision
        def log(phase,**data):
            self.log('prefetch',phase,scope={'epoch':epoch,'utterance_id':utterance,
                'revision':revision,'message_id':self.native_ids.get(utterance,'')},**data)
        log('started',input=query, read_only=True)
        try:
            await asyncio.sleep(self.stable_seconds)  # 和草稿使用同一稳定预算，减少逐字重查。
            result = await self.read(query)
            if (self.closed or epoch != self.epoch or utterance != self.utterance
                    or query != self.query or ticket != self.query_ticket):
                log('discarded',reason='检索完成时话题已改变',output=result)
                return
            previous = self.result
            self.result = result
            admission = await self.host('evidence',epoch=epoch,result=result,evidence_key=evidence_key(result))
            log('completed',input=query,output=result)
            if utterance in self.finals:
                self.log('prefetch','late_adopted', output=result,
                    note='已提交给本轮回复接缝；实际采用看生成或播放记录')
                await self.admit_late_result(previous, result, epoch, utterance,
                    reply_ready_stale=bool(admission.get('reply_ready_stale')))
            elif self.listening:
                self.schedule_speculation()
            if len(result.get('local',{}).get('items',[]))>1 and self.listening:
                await self.clarify('我找到不止一项。你指的是哪一项？可以补充名字。','同一检索词匹配多个事实')
            await self.emit({'type':'heart-rt-state','epoch':epoch,'state':'working',
                             'text':'已开始核对相关记忆；你可以继续说。'})
        except asyncio.CancelledError:
            log('cancelled',input=query,reason='话题改变或插话；远端只读调用可能仍完成')
            raise
        except asyncio.TimeoutError:
            log('timeout', input=query, reason='整个查询超过截止时间', timeout_seconds=self.query_timeout_seconds)
            await self.emit({'type':'heart-rt-state','epoch':epoch,'state':'warning','text':'查询超时；不会把未核实内容当成事实。'})
        except Exception as error:
            log('failed',input=query,reason=type(error).__name__)
            await self.emit({'type':'heart-rt-state','epoch':epoch,'state':'warning',
                             'text':'提前检索未完成，最终回复会说明未核实。'})

    async def final(self, utterance: str, text: str):
        text = text.strip()
        if not text or len(text)>6000:
            raise ValueError('最终转写为空或过长，不能伪造用户输入')
        digest = hashlib.sha256(text.encode()).hexdigest()
        if utterance in self.finals:
            if self.finals[utterance] != digest:
                raise ValueError('同一发言编号出现矛盾的正式文本')
            return {'duplicate':True,'epoch':self.epoch}
        if utterance != self.utterance:
            raise ValueError('旧发言不能提交')
        self.listening = False
        self.final_text = text
        if not self.spec_attempts:
            reason = ('提前生成开关关闭' if not self.preemptive else
                      '文字输入不提前生成' if not self.audio_input else
                      '输入过短、不完整或含需要正式确认的操作' if not eligible(self.partial) else
                      '查询尚未完成' if self.query and self.result is None else
                      '录音结束前未形成足够稳定的临时文字')
            self.log('speculate', 'skipped', reason=reason)
        if self.spec_text and normalize_input(text)!=normalize_input(self.spec_text):
            await self.host('spec_cancel',reason='最终转写修订了临时输入')
            if self.spec_task and not self.spec_task.done():
                self.spec_task.cancel()
            self.spec_text=''
        if self.spec_attempts==0 and self.spec_timer and not self.spec_timer.done():
            self.spec_timer.cancel()
        query = self.intent(text)
        if query != self.query:
            self.query, self.result = query, None
            self.query_ticket += 1
            if self.task and not self.task.done():
                self.task.cancel()
            if query:
                self.task = self.spawn(self.prefetch(self.epoch,utterance,query,self.query_ticket))
        if self.task and not self.task.done():
            # 不让提前检索阻塞反馈；最多等待已开始任务1秒，然后后台结果可纠正。
            _, pending = await asyncio.wait({self.task},timeout=self.query_wait_seconds)
            if pending:
                self.log('prefetch','wait_budget_exceeded', input=query,
                    wait_seconds=self.query_wait_seconds, output='停止等待；后台查询仍在运行')
        compact = re.sub(r'[\s，。！？!?,.]','',text)
        self.log('final','accepted',text=text, output='最终转写交给原生MaiBot角色链路')
        if compact in {'停','停止','停一下','别说了','等一下','等等'}:
            self.finals[utterance] = digest
            self.log('fast','completed',signal=compact,output='停下并等待下一句')
            return {'epoch':self.epoch,'skip_brain':True}
        context = ('[实时插件只读观测；不是用户说的话。未经核实的结果不要当成事实。]\n'
            + json.dumps(self.result,ensure_ascii=False)) if self.result else ''
        if query and not self.result:
            context='[实时插件只读观测：提前检索尚未核实，不得声称查到了结果。若原生工具也未能核实，应明确告诉用户。]'
        await self.host('context', epoch=self.epoch, text=context)
        await self.host('evidence',epoch=self.epoch,result=self.result,evidence_key=evidence_key(self.result))
        approval=await self.host('approve_draft', epoch=self.epoch,utterance_id=utterance,text=text,
            query_pending=bool(query and self.result is None))
        self.log('speculate','approval', approved=bool(approval.get('approved')), input=text,
                 output='最终输入已确认；复用还需通过原生规划与上下文检查')
        await self.host('activate',epoch=self.epoch)
        await self.emit({'type':'heart-rt-active','epoch':self.epoch,'utterance_id':utterance})
        self.finals[utterance] = digest
        # 有界幂等账本；历史凭据仍留在持久化审计中。
        if len(self.finals)>512:
            removed = next(iter(self.finals))
            self.finals.pop(removed)
            self.native_ids.pop(removed, None)
        if query:
            self.watch = self.spawn(self.watch_evidence(self.epoch,utterance,query))
        return {'epoch':self.epoch,'utterance_id':utterance,'duplicate':False}

    async def native_bound(self, utterance: str, message_id: str):
        if utterance != self.utterance or utterance not in self.finals:
            raise ValueError('输入来源编号已过期')
        self.native_ids[utterance] = message_id
        await self.host('native_bound',epoch=self.epoch,message_id=message_id)
        self.log('final','native_bound',output='原生消息已登记；审计按这个编号关联')

    async def register_clip(self, draft_id: str, text: str, group: str, ordinal: int, epoch: int, audio=None):
        if epoch != self.epoch or self.closed:
            raise ValueError('拒绝旧音频')
        if len(self.clips)>=512:
            # 保留本轮账本；丢弃早已关闭的历史世代。
            self.clips = {k:v for k,v in self.clips.items() if v.epoch==self.epoch}
            if len(self.clips)>=512:
                raise ValueError('本轮音频片段过多')
        clip_id = uuid.uuid4().hex
        if audio is not None and type(audio) is not bool:
            raise ValueError('输出媒介必须由VT接缝明确标注')
        self.clips[clip_id] = Clip(clip_id,epoch,draft_id,text,group,ordinal,
                                 requires_audio=self.voice if audio is None else audio)
        self.log('output','queued',clip_id=clip_id,draft_id=draft_id,output=text)
        return {'clip_id':clip_id,'epoch':epoch,'utterance_id':self.utterance,
                'connection_id':self.connection}

    async def receipt(self, clip_id: str, epoch: int, phase: str, position: float=0):
        clip = self.clips.get(clip_id)
        if (clip is None or epoch != clip.epoch or epoch != self.epoch or isinstance(position,bool)
                or not isinstance(position,(float,int)) or not math.isfinite(position) or position<0 or position>300):
            self.log('output','discarded',reason='无效或旧输出播放回执',clip_id=clip_id)
            return {'accepted':False}
        if phase=='progress' and clip.state=='playing':
            if position<clip.position:
                return {'accepted':False}
            clip.position=float(position)
            self.log('output','progress',clip_id=clip_id,position_seconds=position)
            return {'accepted':True}
        if phase=='visible' and clip.requires_audio:
            return {'accepted':False}
        if phase in {'playing','progress','completed'} and not clip.requires_audio:
            return {'accepted':False}
        retry_commit = phase==clip.state and phase in {'completed','visible'} and clip.commit_pending
        if phase==clip.state and not retry_commit:
            return {'accepted':True,'duplicate':True}
        allowed = {'queued':{'playing','visible','failed','stopped'},
                   'playing':{'completed','failed','stopped'},'visible':set()}
        if not retry_commit and phase not in allowed.get(clip.state,set()):
            return {'accepted':False}
        clip.state, clip.position = phase, float(position)
        self.log('output',phase,clip_id=clip_id,output=clip.text,position_seconds=position)
        if phase in {'completed','visible'} and not clip.draft_id.startswith('fast:'):
            peers = sorted((c for c in self.clips.values() if c.group==clip.group),key=lambda c:c.ordinal)
            heard = ''
            for item in peers:
                if item.state not in {'completed','visible'}:
                    break
                heard += item.text
            if heard:
                clip.commit_pending=True
                try:
                    result = await self.host('commit',epoch=epoch,draft_id=clip.draft_id,text=heard)
                    clip.commit_pending=bool(result.get('pending'))
                    self.log('output','commit_pending' if result.get('pending') else 'committed',output=heard,result=result)
                except Exception as error:
                    self.log('failure','commit_failed',output=heard,reason=type(error).__name__)
                    raise
        return {'accepted':True}

    async def admit_late_result(self, previous, result, epoch: int, utterance: str, reply_ready_stale=False):
        if evidence_key(previous)==evidence_key(result) or epoch!=self.epoch or self.listening:
            return
        await self.host('context',epoch=epoch,text='[实时插件最新只读查询材料]\n'+json.dumps(result,ensure_ascii=False))
        # 生成中的结果由 Replyer 接缝处理；已进入展示/播放队列的结果必须明确冻结再更正。
        clips=[c for c in self.clips.values() if c.epoch==epoch and not c.draft_id.startswith('fast:')]
        if (clips or reply_ready_stale) and self.correction_count==0:
            self.correction_count += 1
            packet={'mode':'supplement' if previous is None else 'correct','query':self.query,
                'user_input':self.final_text,'new_evidence':result,
                'completed':[c.text for c in clips if c.state in {'completed','visible'}],
                'interrupted':[{'text':c.text,'seconds':c.position} for c in clips if c.state=='playing']}
            await self.stop('晚到查询补齐或改变了本轮回答的依据')
            await self.host('evidence',epoch=self.epoch,result=result,evidence_key=evidence_key(result))
            await self.host('activate',epoch=self.epoch)
            self.correction_packet=packet
            self.log('correct','requested',output=packet)
            await self.emit({'type':'heart-rt-correction','epoch':self.epoch,
                'utterance_id':utterance,'evidence':packet})

    async def clarify(self, text: str, reason: str):
        if time.monotonic()-self.last_clarify < 5:
            return
        self.last_clarify = time.monotonic()
        self.log('clarify','completed',reason=reason,output=text)
        await self.emit({'type':'heart-rt-clarify','epoch':self.epoch,'text':text})

    async def watch_evidence(self, epoch: int, utterance: str, query: str):
        # 有界读任务；边播放边核实，以实际来源版本区分证据变化。
        started = time.monotonic()
        baseline = self.result
        while time.monotonic()-started < 90 and epoch==self.epoch and not self.closed:
            await asyncio.sleep(2)
            if not self.clips and time.monotonic()-started>30:
                return
            newer = await self.read(query, purpose='回复期间复查证据')
            if epoch!=self.epoch or utterance!=self.utterance or self.closed:
                return
            if baseline is None:
                previous = self.result
                baseline = self.result = newer
                admission=await self.host('evidence',epoch=epoch,result=newer,evidence_key=evidence_key(newer))
                await self.admit_late_result(previous,newer,epoch,utterance,
                    reply_ready_stale=bool(admission.get('reply_ready_stale')))
                continue
            if newer['version'] != baseline['version']:
                self.log('correct','evidence_changed',previous=baseline,output=newer)
                # 只对结构化同一条事实的 value 变化判断矛盾，不能把任意检索排序变化当成反驳。
                old = {x['key']:x['value'] for x in baseline.get('local',{}).get('items',[])}
                changes = [x for x in newer.get('local',{}).get('items',[])
                    if x['key'] in old and x['value']!=old[x['key']]]
                said = ''.join(c.text for c in self.clips.values()
                    if c.epoch==epoch and c.state in {'playing','completed','visible'})
                relevant = [x for x in changes if old[x['key']] in said]
                previous = baseline
                baseline = self.result = newer
                admission=await self.host('evidence',epoch=epoch,result=newer,evidence_key=evidence_key(newer))
                if relevant and self.correction_count==0:
                    self.correction_count += 1
                    packet = {'old':{x['key']:old[x['key']] for x in relevant},'new':relevant,
                              'completed':[c.text for c in self.clips.values() if c.epoch==epoch and c.state in {'completed','visible'}],
                              'interrupted':[{'text':c.text,'seconds':c.position} for c in self.clips.values() if c.epoch==epoch and c.state=='playing']}
                    if self.listening:
                        self.pending_correction = packet
                        self.log('correct','deferred',reason='用户正在说话',output=packet)
                        return
                    await self.stop('核实结果推翻了正在表达的同一条事实')
                    await self.host('activate',epoch=self.epoch)
                    self.log('correct','requested',output=packet)
                    self.correction_packet = packet
                    await self.emit({'type':'heart-rt-correction','epoch':self.epoch,
                        'utterance_id':utterance,'evidence':packet})
                    return
                if admission.get('reply_ready_stale'):
                    await self.admit_late_result(previous,newer,epoch,utterance,reply_ready_stale=True)
                    if epoch!=self.epoch:
                        return
            if self.clips and all(c.state in {'completed','visible','stopped','failed'}
                    for c in self.clips.values() if c.epoch==epoch):
                return

    async def close(self):
        if self.closed:
            return
        self.closed=True
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks,return_exceptions=True)
        await self.host('close')
        self.log('bind','closed',output='连接关闭；不转投QQ或别人的会话')
