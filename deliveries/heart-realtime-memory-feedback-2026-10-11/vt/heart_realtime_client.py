"""VT 的薄接缝；实时策略和审计运行在 MaiBot 的独立插件中。"""
from contextvars import ContextVar
from pathlib import Path

import asyncio
import json
import os
import time
import uuid

from aiohttp import ClientSession, ClientTimeout, WSMsgType


turn = ContextVar('heart_rt_turn',default=None)
draft = ContextVar('heart_rt_draft',default=None)
LINKS = {}
MODES = {}


def plugin_root():
    """按本机配置寻找插件，允许伙伴使用不同目录或重命名插件文件夹。"""
    explicit=os.environ.get('HEART_REALTIME_PLUGIN_DIR')
    if explicit:
        return Path(explicit).expanduser().resolve()
    maibot=os.environ.get('HEART_MAIBOT_ROOT')
    if maibot:
        return (Path(maibot).expanduser()/'plugins/heart-realtime').resolve()
    raise RuntimeError('请配置 HEART_MAIBOT_ROOT 或 HEART_REALTIME_PLUGIN_DIR，再开启实时插件')


def record_mode(uid, phase, enabled, preemptive, output):
    from heart_bridge import _active_bridge
    if _active_bridge:
        _active_bridge.store.append('实时对话插件','',plugin='heart.realtime',feature='插件开关',
            phase=phase, visitor_id=uid, plugin_enabled=enabled,
            session_enabled=enabled, preemptive_enabled=preemptive,
            output=output)


def observe_turn_mode(uid, trace):
    link=LINKS.get(uid)
    enabled=bool(link and link.alive)
    preemptive=bool(enabled and MODES.get(uid,{}).get('preemptive'))
    trace.stage('realtime_plugin',plugin='heart.realtime',plugin_enabled=enabled,session_enabled=enabled,
        preemptive_enabled=preemptive,status='实时插件开启；提前生成'+('开启' if preemptive else '关闭')
            if enabled else '实时插件关闭；本轮使用普通链路')


class Link:
    def __init__(self,uid,handler,websocket):
        self.uid,self.handler,self.web = uid,handler,websocket
        self.http=None
        self.socket=None
        self.reader=None
        self.heartbeat=None
        self.pending={}
        self.serial=0
        self.epoch=0
        self.utterance=''
        self.session=''
        self.closed=False
        self.voice=True
        self.ack_audio=None
        self.ack_task=None
        self.tasks=set()
        self.listening=False
        self.connection=''
        self.needs_fence_reset=True
        self.supports_input_end=False

    @property
    def alive(self):
        return bool(not self.closed and self.socket and not self.socket.closed
                    and self.reader and not self.reader.done())

    def spawn(self,coro):
        from loguru import logger
        task=asyncio.create_task(coro)
        self.tasks.add(task)
        def done(t):
            self.tasks.discard(t)
            if not t.cancelled() and t.exception():
                logger.error('实时接缝任务失败：{}',type(t.exception()).__name__)
        task.add_done_callback(done)
        return task

    async def open(self):
        root=plugin_root()
        location=json.loads((root/'runtime-location.json').read_text(encoding='utf-8'))
        key=Path(location['key_path']).read_text(encoding='utf-8')
        self.http=ClientSession(timeout=ClientTimeout(total=10))
        async with self.http.get(f'http://127.0.0.1:{location["port"]}/health') as response:
            response.raise_for_status()
            health=await response.json()
            self.supports_input_end=health.get('input_end_supported') is True
        self.socket=await self.http.ws_connect(f'http://127.0.0.1:{location["port"]}/bridge',
            headers={'X-Heart-Realtime':key},heartbeat=15,max_msg_size=128*1024)
        self.reader=asyncio.create_task(self.receive())
        binding=await self.request('bind',visitor_id=self.uid)
        self.session=binding['session_id']
        self.connection=binding['connection_id']
        self.heartbeat=self.spawn(self.keepalive())

    async def warm_ack(self):
        from voice_context import client_uid
        from src.open_llm_vtuber.utils.stream_audio import prepare_audio_payload
        from src.open_llm_vtuber.agent.output_types import DisplayText
        context=self.handler.client_contexts[self.uid]
        voice_token=client_uid.set(self.uid)
        path=None
        try:
            path=await context.tts_engine.async_generate_audio(text='我在，正在听。',
                file_name_no_ext='rt_ack_'+uuid.uuid4().hex)
            self.ack_audio=prepare_audio_payload(audio_path=path,display_text=DisplayText(
                text='我在，正在听。',name=context.character_config.character_name),actions=None)
        except Exception as error:
            self.ack_audio=None  # 快反应文字仍会出现，失败不得冒充声音播放成功。
            await self.request('feature_report',feature='fast',phase='voice_prepare_failed',
                output='短句应答声带准备失败：'+type(error).__name__,medium='text')
        finally:
            if path:context.tts_engine.remove_file(path)
            client_uid.reset(voice_token)

    async def quick_audio(self,event):
        if not self.voice or not self.ack_audio:
            await self.request('feature_report',feature='fast',phase='text_feedback',
                output=event['text'],medium='text')
            return
        # 预合成的同一声带只用于约定的点名应答，不能缓存变化中的事实。
        if event['text']!='我在，正在听。':
            return
        result=await self.request('clip',draft_id='fast:ack:'+str(self.epoch),text=event['text'],
            group='fast:'+uuid.uuid4().hex,ordinal=0,epoch=self.epoch,audio=True)
        if event['epoch']==self.epoch and not self.closed:
            await self.web.send_text(json.dumps({**self.ack_audio,'heart_rt':dict(result,kind='ack'),
                'heart_voice_enabled':True,'heart_audio_status':'ready'}))

    async def clarify_audio(self,event):
        if not self.voice:
            return
        from voice_context import client_uid
        from src.open_llm_vtuber.utils.stream_audio import prepare_audio_payload
        from src.open_llm_vtuber.agent.output_types import DisplayText
        context=self.handler.client_contexts[self.uid]
        token=client_uid.set(self.uid)
        path=None
        try:
            path=await context.tts_engine.async_generate_audio(text=event['text'],
                file_name_no_ext='rt_clarify_'+uuid.uuid4().hex)
            if self.closed or not self.listening or self.epoch!=event['epoch']:
                return
            payload=prepare_audio_payload(audio_path=path,display_text=DisplayText(
                text=event['text'],name=context.character_config.character_name),actions=None)
            receipt=await self.request('clip',draft_id='fast:clarify:'+str(self.epoch),
                text=event['text'],group='fast:'+uuid.uuid4().hex,ordinal=0,epoch=self.epoch,audio=bool(payload.get('audio')))
            await self.web.send_text(json.dumps({**payload,'heart_rt':dict(receipt,kind='ack'),
                'heart_voice_enabled':True,'heart_audio_status':'ready'}))
            await self.request('feature_report',feature='clarify',phase='voice_queued',
                output=event['text'],medium='audio')
        except Exception as error:
            await self.request('feature_report',feature='clarify',phase='voice_failed',
                output=type(error).__name__,medium='text')
        finally:
            if path:context.tts_engine.remove_file(path)
            client_uid.reset(token)

    async def keepalive(self):
        while not self.closed:
            await asyncio.sleep(10)
            await self.request('heartbeat')

    async def request(self,action,**payload):
        if self.closed or not self.socket or self.socket.closed or (self.reader and self.reader.done()):
            raise RuntimeError('实时插件连接已结束')
        self.serial+=1
        key=str(self.serial)
        future=asyncio.get_running_loop().create_future()
        self.pending[key]=future
        try:
            await self.socket.send_json(dict(id=key,action=action,**payload))
            result=await asyncio.wait_for(future,timeout=10)
            return result
        finally:
            self.pending.pop(key,None)

    async def receive(self):
        try:
            async for msg in self.socket:
                if msg.type!=WSMsgType.TEXT:
                    continue
                packet=msg.json()
                if 'id' in packet:
                    future=self.pending.get(packet['id'])
                    if future and not future.done():
                        if packet.get('ok'):
                            future.set_result(packet['result'])
                        else:
                            future.set_exception(RuntimeError(packet.get('error','实时请求失败')))
                elif 'event' in packet:
                    event=packet['event']
                    if event['type']=='heart-rt-stop':
                        self.epoch=event['epoch']
                        task=self.handler.current_conversation_tasks.get(self.uid)
                        if task and not task.done():
                            task.cancel()
                    await self.web.send_text(json.dumps(event,ensure_ascii=False))
                    if event['type']=='heart-rt-ack':
                        self.spawn(self.quick_audio(event))
                    if event['type']=='heart-rt-clarify':
                        self.spawn(self.clarify_audio(event))
                    if event['type']=='heart-rt-correction':
                        # 启动纠正用同一个 MaiBot 人格/记忆；不制造一条用户发言。
                        from src.open_llm_vtuber.conversations.single_conversation import process_single_conversation
                        metadata={'heart_turn_id':str(uuid.uuid4()),'heart_voice_enabled':self.voice,
                            'heart_rt_epoch':event['epoch'],'heart_rt_utterance':self.utterance,
                            'heart_rt_correction':True,'skip_history':True}
                        task=asyncio.create_task(process_single_conversation(
                            context=self.handler.client_contexts[self.uid],websocket_send=self.web.send_text,
                            client_uid=self.uid,user_input='[正在纠正先前表达，属于系统任务]',metadata=metadata))
                        self.handler.current_conversation_tasks[self.uid]=task
        finally:
            was_closed=self.closed
            self.closed=True
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(ConnectionError('实时插件断开'))
            if not was_closed:
                try:
                    await self.web.send_text(json.dumps({'type':'heart-rt-link-lost',
                        'text':'实时连接已断开；完整输入将走普通链路，下一轮重新连接。'}))
                except Exception:
                    pass  # 网页自身也已断开时只清理连接，不制造新的回复。

    async def close(self):
        if self.socket and not self.socket.closed and self.reader and not self.reader.done():
            try:
                await self.request('disable')
            except Exception:
                pass  # 断线时宿主输出租约仍会冻结；不把失败包装成成功。
        self.closed=True
        if self.heartbeat:
            self.heartbeat.cancel()
        if self.ack_task:
            self.ack_task.cancel()
            await asyncio.gather(self.ack_task,return_exceptions=True)
        for task in list(self.tasks):
            task.cancel()
        await asyncio.gather(*self.tasks,return_exceptions=True)
        if self.socket:
            await self.socket.close()
        if self.reader:
            await asyncio.gather(self.reader,return_exceptions=True)
        if self.http:
            await self.http.close()


async def event(handler,websocket,uid,data):
    """只接受当前实际WebSocket的身份；不信任浏览器提供的session/user编号。"""
    if uid not in handler.client_contexts or handler.client_connections.get(uid) is not websocket:
        raise PermissionError('网页连接未登记')
    group=handler.chat_group_manager.get_client_group(uid)
    if group and len(group.members)>1:
        raise ValueError('Live2D多人语音模式尚未开放；QQ多人聊天不受影响')
    action=data.get('action')
    if action=='mode':
        if type(data.get('enabled')) is not bool or type(data.get('preemptive')) is not bool:
            raise ValueError('实时模式开关必须是布尔值')
        enabled,preemptive=data['enabled'],data['enabled'] and data['preemptive']
        MODES[uid]={'enabled':enabled,'preemptive':preemptive}
        record_mode(uid,'requested',enabled,preemptive,'用户在Live2D界面请求修改插件开关')
        if not enabled:
            await close(uid)
            await websocket.send_text(json.dumps({'type':'heart-rt-mode','enabled':False,'preemptive':False}))
            return
    link=LINKS.get(uid)
    fresh=False
    if link and not link.alive:
        desired=dict(MODES.get(uid,{}))
        LINKS.pop(uid,None)
        await link.close()
        MODES[uid]=desired
        link=None
    if link is None and action in {'audio','receipt','input_end','stop'}:
        return  # 失联期间不回放旧录音/回执；下一次 begin 才重新绑定。
    if link is None:
        link=Link(uid,handler,websocket)
        try:
            await link.open()
        except Exception:
            await link.close()
            if action=='mode':
                MODES.pop(uid,None)
                record_mode(uid,'failed',False,False,'实时插件未能开启；保留普通链路')
                await websocket.send_text(json.dumps({'type':'heart-rt-mode','enabled':False,'preemptive':False}))
            raise
        LINKS[uid]=link
        fresh=True
    if action=='mode':
        await link.request('mode',preemptive=MODES[uid]['preemptive'])
        record_mode(uid,'changed',True,MODES[uid]['preemptive'],'实时插件已按界面选择启用')
        await websocket.send_text(json.dumps({'type':'heart-rt-mode','enabled':True,
            'preemptive':MODES[uid]['preemptive']}))
    elif action=='begin':
        await link.request('mode',preemptive=bool(MODES.get(uid,{}).get('preemptive',False)))
        link.listening=True
        link.voice=data.get('voice',True) is True
        if link.voice and not link.ack_audio and (link.ack_task is None or link.ack_task.done()):
            link.ack_task=link.spawn(link.warm_ack())
        result=await link.request('begin',utterance_id=data['utterance_id'],voice=link.voice,
                                  audio=data.get('audio',False))
        link.utterance,link.epoch=data['utterance_id'],result['epoch']
        await websocket.send_text(json.dumps({'type':'heart-rt-bound',**result,
            'connection_id':link.connection,'new_connection':link.needs_fence_reset}))
        link.needs_fence_reset=False
    elif action=='audio':
        await link.request('audio',utterance_id=data['utterance_id'],samples=data['samples'])
    elif action=='input_end':
        link.listening=False
        if link.supports_input_end:
            await link.request('input_end',utterance_id=data['utterance_id'])
    elif action=='stop':
        link.epoch=(await link.request('stop'))['epoch']
    elif action=='receipt':
        # 浏览器回执只允许本连接当前plugin登记的clip，不接受直接提交正文。
        await link.request('receipt',clip_id=data['clip_id'],epoch=data['epoch'],
                            phase=data['phase'],position=data.get('position',0))
    elif action=='disable':
        await close(uid)
    else:
        raise ValueError('网页不能调用宿主查询、提交或纠正接口')


async def close(uid):
    link=LINKS.pop(uid,None)
    if link:
        await link.close()
    MODES.pop(uid,None)


async def final_input(uid,text,metadata):
    link=LINKS.get(uid)
    utterance=metadata.get('heart_rt_utterance')
    if not link or not utterance:
        return None
    from dialogue_trace import current_turn
    trace=current_turn.get()
    if trace:
        trace.stage('realtime_correlation',_kind='实时对话插件',plugin='heart.realtime',
            feature='输入关联',phase='bound',utterance_id=utterance,
            plugin_enabled=link.alive,session_enabled=link.alive,
            preemptive_enabled=bool(MODES.get(uid,{}).get('preemptive')))
    link.listening=False
    try:
        if not link.alive:
            raise ConnectionError('实时连接已断开')
        result=await link.request('final',utterance_id=utterance,text=text)
    except (ConnectionError, asyncio.TimeoutError, RuntimeError) as error:
        # 只在传输确实断开时退回完整输入。宿主业务错误保持报错，不掩盖错误。
        if link.alive and not isinstance(error, (ConnectionError, asyncio.TimeoutError)):
            raise
        if trace:
            trace.stage('realtime_fallback',plugin='heart.realtime',plugin_enabled=False,
                preemptive_enabled=False,status='实时连接中断；本轮完整转写使用普通链路',
                reason=type(error).__name__)
        return None
    if result.get('duplicate') or result.get('skip_brain'):
        return {'skip_brain':True}
    return {'link':link,'epoch':result['epoch'],'utterance_id':utterance}


async def bind_native(message_id):
    current=turn.get()
    if current:
        await current['link'].request('native_bound',utterance_id=current['utterance_id'],
                                      message_id=message_id)


async def received_draft(text):
    current=turn.get()
    draft.set(None)
    if not current:
        return
    deadline=time.monotonic()+2
    delivery_key=uuid.uuid4().hex
    while time.monotonic()<deadline:
        result=await current['link'].request('draft',text=text,delivery_key=delivery_key)
        if result.get('draft_id'):
            draft.set({'id':result['draft_id'],'original':result['text'],**current})
            return
        await asyncio.sleep(.05)
    raise RuntimeError('回复未登记成实时草稿，停止播报以免记录未听到的内容')


async def annotate_payload(payload,ordinal):
    current=turn.get()
    item=draft.get()
    if not current or not item:
        return True
    link=current['link']
    if link.epoch!=current['epoch']:
        return False
    text=payload.get('display_text',{}).get('text','')
    receipt=await link.request('clip',draft_id=item['id'],text=text,group=item['id'],
                               ordinal=ordinal,epoch=current['epoch'],audio=bool(payload.get('audio')))
    payload['heart_rt']=receipt
    return True


async def cancel_current(current):
    if current and not current['link'].closed and not current['link'].socket.closed and current['link'].epoch==current['epoch']:
        try:
            await current['link'].request('stop')
        except (ConnectionError,RuntimeError):
            if not current['link'].closed and not current['link'].socket.closed:
                raise


def eligible_payload(payload):
    """在 WebSocket 发送前再验一次世代；排队期间发生插话也不会漏出旧声音。"""
    current=turn.get()
    return not current or current['link'].epoch==current['epoch']


async def prepare_correction():
    current=turn.get()
    if current:
        await current['link'].request('correct_generate',epoch=current['epoch'])
