"""独立 MaiBot Lv4.5 插件：通过受信任 VT 接缝管理实时听、查、停、说、改。"""
from pathlib import Path
from typing import Any, Dict

import asyncio
import json
import logging
import os
import re
import secrets
import struct
import subprocess
import time
import uuid

from aiohttp import ClientSession, ClientTimeout, web, WSMsgType
from maibot_sdk import HomeCard, HookHandler, MaiBotPlugin
from maibot_sdk.types import HookMode, HookOrder

from heart_shared.card_writer import start_writer
from heart_shared.storage import AuditStore, clean
from heart_shared.console_view import console_summary

from .engine import Conversation
from .runtime_config import DEFAULTS, VERSION, binding_names, check_asr_runtime, resolve_settings


ROOT = Path(__file__).resolve().parent


class HeartRealtimePlugin(MaiBotPlugin):
    def __init__(self):
        super().__init__()
        self.settings = dict(DEFAULTS)
        self.connections: Dict[str,Conversation] = {}
        self.sockets = {}
        self.asr = {}
        self.http = None
        self.runner = None
        self.child = None
        self.child_log = None
        self.key = ''
        self.store = None
        self.asr_ready = False
        self.asr_instance = uuid.uuid4().hex
        self.tasks = set()
        self.asr_diagnostic = None

    async def on_load(self):
        if (ROOT/'settings.json').is_file():
            self.settings.update(json.loads((ROOT/'settings.json').read_text(encoding='utf-8')))
        self.settings = resolve_settings(ROOT,self.settings)
        self.store = AuditStore()
        start_writer(self.store)
        self.ctx.paths.runtime_dir.mkdir(parents=True,exist_ok=True)
        self.ctx.paths.data_dir.mkdir(parents=True,exist_ok=True)
        self.audit('插件开关','',phase='loaded',plugin_enabled=False,
            session_enabled=False,preemptive_enabled=False,output='会话开关默认关闭，由Live2D界面选择')
        if not self.settings['enabled']:
            self.ctx.logger.info('Heart 实时插件已禁用；普通聊天保持原链路')
            return
        key_path = self.ctx.paths.runtime_dir/'bridge-key'
        self.key = secrets.token_urlsafe(32)
        key_path.write_text(self.key,encoding='utf-8')
        # VT 从固定路径读取凭据；不放进网页、日志或URL。
        (ROOT/'runtime-location.json').write_text(json.dumps({'key_path':str(key_path),
            'port':self.settings['port']},ensure_ascii=False),encoding='utf-8')
        self.http = ClientSession(timeout=ClientTimeout(total=6))
        app = web.Application(client_max_size=256*1024)
        app.router.add_get('/health',self.health)
        app.router.add_get('/bridge',self.bridge)
        self.runner=web.AppRunner(app)
        await self.runner.setup()
        try:
            await web.TCPSite(self.runner,'127.0.0.1',int(self.settings['port'])).start()
        except Exception:
            await self.runner.cleanup()
            await self.http.close()
            raise
        self.spawn(self.start_asr())
        self.ctx.logger.info('Heart 实时插件已加载：每项功能独立审计；QQ只走原生文字回复')

    async def on_config_update(self, scope: str, config_data: Dict[str, Any], version: str) -> None:
        # 端口与模型进程不能在进行中的对话里偷偷替换；SDK配置通知明确提示重载。
        if scope == 'self':
            self.ctx.logger.info('实时插件收到配置通知（%s）；settings.json修改后请重载插件', version)

    def spawn(self,coro):
        task=asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        def done(t):
            if not t.cancelled() and t.exception():
                self.audit('故障隔离','',phase='failed',reason=type(t.exception()).__name__)
        task.add_done_callback(done)
        return task

    async def start_asr(self):
        self.asr_diagnostic = await asyncio.to_thread(check_asr_runtime,ROOT,self.settings)
        self.audit('兼容性检查','',phase='completed' if self.asr_diagnostic['ready'] else 'failed',
                   output=self.asr_diagnostic)
        if not self.asr_diagnostic['ready']:
            return
        environment = dict(os.environ,HEART_RT_KEY=self.key,HEART_RT_MODEL=str(self.settings['model']),
                           HEART_RT_ASR_PORT=str(self.settings['asr_port']),
                           HEART_RT_ASR_INSTANCE=self.asr_instance,
                           HEART_RT_ASR_DEPS=str(self.settings['asr_dependencies']))
        self.child_log=(self.ctx.paths.data_dir/'stream-asr.log').open('a',encoding='utf-8')
        self.child=subprocess.Popen([self.settings['asr_python'],'-u',str(ROOT/'stream_asr.py')],
            env=environment,stdin=subprocess.DEVNULL,stdout=self.child_log,stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        # 只启动本插件拥有的流式进程；退出时不按端口杀别人的服务。
        deadline=time.monotonic()+75
        while time.monotonic()<deadline and self.child.poll() is None:
            try:
                async with self.http.get(f'http://127.0.0.1:{self.settings["asr_port"]}/health') as response:
                    data=await response.json()
                    if (data.get('service')=='heart-streaming-zipformer' and data.get('ready')
                            and data.get('instance')==self.asr_instance):
                        self.asr_ready=True
                        self.audit('流式听取','',phase='ready',output=data)
                        return
            except (OSError, asyncio.TimeoutError):
                pass
            await asyncio.sleep(.25)
        self.audit('故障隔离','',phase='failed',reason='流式模型未就绪；保留普通语音与文字链路')

    def audit(self,feature,session,**data):
        payload=clean(dict(plugin='heart.realtime',feature=feature,
            **{'plugin_loaded':True,'plugin_config_enabled':bool(self.settings['enabled']),'plugin_enabled':False,
               'session_enabled':False,'preemptive_enabled':False,**data}))
        # 日常控制台不重抄每帧转写/协议字段；完整凭证仍在事件库和插件JSONL。
        summary = console_summary(feature, payload)
        if summary:
            self.ctx.logger.info('实时插件 | %s', summary)
        day=time.strftime('%Y-%m-%d')
        with (self.ctx.paths.data_dir/f'{day}-realtime.jsonl').open('a',encoding='utf-8') as f:
            f.write(json.dumps(dict(at=time.time(),session_id=session,**payload),ensure_ascii=False)+'\n')
        if self.store:
            self.store.append('实时对话插件',session,**payload)

    async def local_facts(self,query):
        path=(ROOT/self.settings['facts_file']).resolve()
        if not path.is_relative_to(ROOT.resolve()):
            raise ValueError('事实来源必须在本插件目录内')
        data=await asyncio.to_thread(path.read_text,encoding='utf-8')
        facts=json.loads(data)
        if not isinstance(facts,dict) or len(facts)>256:
            raise ValueError('事实表格式错误')
        items=[]
        for key,item in facts.items():
            if not isinstance(item,dict) or set(item)-{'value','version','aliases'}:
                raise ValueError('事实表条目格式错误')
            aliases=[key]+list(item.get('aliases',[]))
            if any(str(alias) and str(alias) in query for alias in aliases):
                value=str(item['value'])[:1500]
                items.append({'key':key,'value':value,'version':str(item.get('version',''))})
        return {'source':'本地管理员事实表','items':items[:5]}

    async def call(self,name,**kwargs):
        if name=='local.facts':
            return await self.local_facts(kwargs['query'])
        return await self.ctx.call_capability(name,timeout_ms=30000 if kwargs.get('action')=='speculate' else 7000,**kwargs)

    async def health(self,request):
        return web.json_response({'service':'heart.realtime','ready':True,'streaming_ready':self.asr_ready,
                                 'connections':len(self.connections),'version':VERSION,
                                 'asr_diagnostic':self.asr_diagnostic,
                                 'plugin_enabled':self.settings['enabled'],'route':'bounded-preemptive',
                                 'input_end_supported':True,'connection_fence_supported':True})

    async def start_stream(self,connection,utterance):
        await self.finish_stream(connection,False)
        if not self.asr_ready:
            raise RuntimeError('流式识别尚未就绪；可以使用原普通语音输入')
        socket=await self.http.ws_connect(f'http://127.0.0.1:{self.settings["asr_port"]}/stream',
            headers={'X-Heart-Realtime':self.key},heartbeat=10,max_msg_size=65536)
        async def receive():
            async for msg in socket:
                if msg.type==WSMsgType.TEXT:
                    body=msg.json()
                    if body.get('error'):
                        raise RuntimeError(body['error'])
                    await self.connections[connection].partial_input(utterance,body['revision'],body['text'])
        task=self.spawn(receive())
        self.asr[connection]=socket,task

    async def finish_stream(self,connection,flush=True):
        entry=self.asr.pop(connection,None)
        if entry:
            socket,task=entry
            if flush and not socket.closed:
                await socket.send_str('finish')
                await asyncio.wait({task},timeout=1)
            await socket.close()
            if not task.done():
                task.cancel()
            await asyncio.gather(task,return_exceptions=True)

    async def bridge(self,request):
        if request.headers.get('Origin') or not secrets.compare_digest(
                request.headers.get('X-Heart-Realtime',''),self.key):
            raise web.HTTPForbidden(text='仅允许受信任的本机VT接缝')
        if len(self.connections)>=4:
            raise web.HTTPServiceUnavailable(text='实时连接已满')
        socket=web.WebSocketResponse(max_msg_size=256*1024,heartbeat=15)
        await socket.prepare(request)
        connection=uuid.uuid4().hex
        conversation=None
        async def emit(packet):
            if not socket.closed:
                await socket.send_json({'event':dict(packet,connection_id=connection)})
        try:
            async for msg in socket:
                if msg.type!=WSMsgType.TEXT:
                    continue
                body=msg.json()
                request_id=body.get('id')
                try:
                    action=body.get('action')
                    if action=='bind' and conversation is None:
                        visitor=str(body.get('visitor_id') or '')
                        if not re.fullmatch(r'[0-9a-f-]{36}',visitor):
                            raise ValueError('无效的原生网页连接编号')
                        opened=await self.ctx.chat.open_session(platform='webui',chat_type='private',
                            user_id='webui_user_vtuber_visit_'+visitor.replace('-',''))
                        session=opened['session_id']
                        conversation=Conversation(session,visitor,connection,self.call,emit,self.audit)
                        for name in ('stable_seconds','query_wait_seconds','query_timeout_seconds'):
                            value=float(self.settings[name])
                            if not .05<=value<=10:
                                raise ValueError('实时时间预算必须在0.05到10秒之间')
                            setattr(conversation,name,value)
                        binding = await conversation.host('bind')
                        conversation.character_names = binding_names(binding,self.settings)
                        self.connections[connection]=conversation
                        self.sockets[connection]=socket
                        conversation.log('bind','completed',output='Live2D绑定独立原生会话')
                        result={'session_id':session,'connection_id':connection,'epoch':0}
                    elif conversation is None:
                        raise ValueError('需要先绑定')
                    elif action=='begin':
                        result=await conversation.begin(body['utterance_id'],body.get('voice',True),body.get('audio',False))
                        if body.get('audio',False) and not result.get('duplicate'):
                            await self.start_stream(connection,body['utterance_id'])
                    elif action=='audio':
                        if body['utterance_id']!=conversation.utterance or not conversation.listening:
                            raise ValueError('旧录音帧')
                        frames=body['samples']
                        if not isinstance(frames,list) or len(frames)>8000:
                            raise ValueError('录音帧过大')
                        import math
                        if any(isinstance(x,bool) or not isinstance(x,(int,float)) or not math.isfinite(x)
                            or abs(x)>1.01 for x in frames):
                            raise ValueError('录音帧包含无效数据')
                        await self.asr[connection][0].send_bytes(struct.pack('<'+'f'*len(frames),*frames))
                        result={'accepted':True}
                    elif action=='input_end':
                        result=await conversation.input_end(body['utterance_id'])
                    elif action=='mode':
                        await conversation.set_mode(body['preemptive'])
                        result={'preemptive':conversation.preemptive,'session_enabled':True}
                    elif action=='final':
                        await self.finish_stream(connection)
                        result=await conversation.final(body['utterance_id'],body['text'])
                    elif action=='native_bound':
                        await conversation.native_bound(body['utterance_id'],body['message_id'])
                        result={'accepted':True}
                    elif action=='draft':
                        result=await conversation.host('draft',text=body['text'],delivery_key=body.get('delivery_key',''))
                    elif action=='clip':
                        result=await conversation.register_clip(body['draft_id'],body['text'],
                            body['group'],body['ordinal'],body['epoch'],body.get('audio'))
                    elif action=='receipt':
                        result=await conversation.receipt(body['clip_id'],body['epoch'],body['phase'],
                            body.get('position',0))
                    elif action=='clarify':
                        await conversation.clarify(body['text'],body['reason'])
                        result={'accepted':True}
                    elif action=='stop':
                        await conversation.stop('用户中断或VT取消')
                        await self.finish_stream(connection,False)
                        result={'epoch':conversation.epoch}
                    elif action=='heartbeat':
                        result=await conversation.host('heartbeat')
                    elif action=='feature_report':
                        feature=body.get('feature')
                        if feature not in {'fast','clarify','failure'}:
                            raise ValueError('接缝不能伪造宿主操作记录')
                        conversation.log(feature,str(body.get('phase','observed'))[:60],
                            output=str(body.get('output',''))[:1500],medium=body.get('medium',''))
                        result={'recorded':True}
                    elif action=='correct_generate':
                        packet=conversation.correction_packet
                        if packet is None or body.get('epoch')!=conversation.epoch:
                            raise ValueError('没有本轮核实并批准的更正任务')
                        conversation.correction_packet=None
                        instruction=('【后台事实补充或更正任务，不能当成用户发言】\n'
                            '请保持原角色与心情。mode=supplement代表首次查询晚到，应补充已核实信息，'
                            '没有证据不能声称先前说错。其他mode代表证据改变，应收回确实被推翻的旧值。'
                            '根据本次材料回应原问题，不能继续播放旧稿。'
                            '只有completed里的句子收到完整播放回执；interrupted只知道播放秒数，'
                            '不能声称用户听完或理解了。\n'+json.dumps(packet,ensure_ascii=False))
                        await conversation.host('context',epoch=conversation.epoch,text=instruction)
                        result=await conversation.host('correct_generate',epoch=conversation.epoch,intent=instruction)
                    elif action=='disable':
                        await self.finish_stream(connection,False)
                        await conversation.close()
                        result={'closed':True}
                    else:
                        raise ValueError('不支持的实时插件功能')
                    await socket.send_json({'id':request_id,'ok':True,'result':result})
                    if action=='disable':
                        break
                except Exception as error:
                    self.audit('故障隔离',conversation.session if conversation else '',phase='failed',
                        reason=type(error).__name__,action=body.get('action'))
                    await socket.send_json({'id':request_id,'ok':False,'error':str(error)[:400]})
        finally:
            await self.finish_stream(connection,False)
            if conversation:
                try:
                    await conversation.close()
                except Exception as error:
                    self.audit('故障隔离',conversation.session,phase='close_failed',reason=type(error).__name__)
            self.connections.pop(connection,None)
            self.sockets.pop(connection,None)
        return socket

    @HookHandler('chat.receive.before_process',name='heart_realtime_qq_boundary',
        description='QQ消息归属检查与实时功能边界审计，不修改或取消QQ消息',
        mode=HookMode.OBSERVE,order=HookOrder.LATE,timeout_ms=1000)
    async def qq_boundary(self,message:Dict[str,Any]|None=None,**kwargs):
        if not message or not self.store:
            return
        fields=self.store.message_fields(message)
        info=message.get('message_info') or {}
        if str(info.get('platform') or '').lower()!='qq':
            return
        self.audit('QQ多人隔离',fields['session'],phase='observed',message_id=fields['message_id'],
            session_enabled=False,preemptive_enabled=False,
            sender_id=fields['user'],chat_type=fields['chat_type'],
            group_id=str((info.get('group_info') or {}).get('group_id','')),
            input=fields['text'],chat_title=fields['title'],
            output='保留原生消息归属；不触发Live2D插话，不启用QQ语音输出')

    @HomeCard(name='heart_realtime_status',title='实时对话插件',description='Lv4.5状态与功能日志',
        content='查看流式听取、只读查询、插话和播放账本状态。',icon='☎️',width='medium',order=101)
    async def status(self):
        return {'blocks':[{'type':'key_value','entries':{'插件':'Heart实时对话 '+VERSION,
            '提前生成':'由Live2D开关控制，每轮最多一次；草稿不提前播放',
            '流式识别':'已就绪' if self.asr_ready else '未就绪/未启用',
            'Live2D连接':str(len(self.connections)),'QQ':'原生文字回复，多人消息独立归属',
            '观测':'Heart日志 + 插件逐功能JSONL'}}]}

    async def on_unload(self):
        for socket in list(self.sockets.values()):
            await socket.close()
        if self.runner:
            await self.runner.cleanup()
        for task in list(self.tasks):
            task.cancel()
        await asyncio.gather(*self.tasks,return_exceptions=True)
        if self.child and self.child.poll() is None:
            # Windows venv launcher may have a child interpreter: terminate this owned tree only.
            if os.name=='nt':
                process=await asyncio.create_subprocess_exec('taskkill','/PID',str(self.child.pid),'/T','/F',
                    stdout=asyncio.subprocess.DEVNULL,stderr=asyncio.subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW)
                await process.wait()
            else:
                self.child.terminate()
            await asyncio.to_thread(self.child.wait,timeout=5)
        if self.child_log:
            self.child_log.close()
        if self.http:
            await self.http.close()
        self.ctx.logger.info('Heart实时插件已卸载；只关闭本插件连接与拥有的流式进程')


def create_plugin():
    return HeartRealtimePlugin()
