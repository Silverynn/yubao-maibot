"""Heart 实时插件的宿主接缝：只接管已绑定的 Live2D 原生会话。"""
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Dict

import asyncio
import hashlib
import json
import re
import time
import uuid


@dataclass
class Lease:
    visitor: str
    connection: str
    epoch: int = 0
    active: bool = False
    touched: float = field(default_factory=time.monotonic)
    drafts: Dict[str, Any] = field(default_factory=dict)
    committed: Dict[str, str] = field(default_factory=dict)
    cancel_pending: bool = False
    reservations: Dict[str, str] = field(default_factory=dict)
    preemptive: bool = False
    utterance: str = ''
    native_id: str = ''
    prepared: Any = None
    speculation_count: int = 0
    evidence: Any = None
    evidence_key: str = ''
    evidence_changed: asyncio.Event = field(default_factory=asyncio.Event)
    reply_evidence_key: Any = None


LEASES: Dict[str, Lease] = {}


def is_managed(message: Any) -> bool:
    return message.platform == 'webui' and message.session_id in LEASES


def speech_text(message: Any) -> str:
    """朗读的是原生文本组件；引用的问题和富媒体预览不能混入输出账本。"""
    from src.plugin_runtime.host.message_utils import PluginMessageUtils

    segments = PluginMessageUtils._message_sequence_to_dict(message.raw_message)
    return ''.join(item['data'] for item in segments if item['type']=='text').strip()


def defer_delivery(message: Any) -> bool:
    """输送草稿不等于用户听到；没有启用实时插件的会话保持原逻辑。"""
    if not is_managed(message):
        return False
    lease = LEASES[message.session_id]
    if lease.active and time.monotonic() - lease.touched < 180:
        lease.drafts[str(message.message_id)] = (lease.epoch, deepcopy(message))
    return True


def can_deliver(message: Any) -> bool:
    if not is_managed(message):
        return True
    lease = LEASES[message.session_id]
    return lease.active and time.monotonic() - lease.touched < 180


def resolve_session(session_id: str, visitor: str):
    from src.chat.message_receive.chat_manager import chat_manager

    session = chat_manager.get_existing_session_by_session_id(session_id)
    expected = 'webui_user_vtuber_visit_' + visitor.replace('-', '').lower()
    if (session is None or session.platform != 'webui' or session.is_group_session
            or not re.fullmatch(r'[0-9a-f-]{36}', visitor) or session.user_id != expected):
        raise PermissionError('只允许本人已登记的 Live2D 私聊；QQ与普通WebUI不接管')
    return session


async def cancel_runtime(session_id: str) -> Dict[str, Any]:
    from src.chat.heart_flow.heartflow_manager import heartflow_manager

    runtime = heartflow_manager.heartflow_chat_list.get(session_id)
    if runtime is None:
        return {'local_cancelled': True, 'remote_status': '未启动推理'}
    # 取消完整的会话推理循环，包括 Replyer 与正在 await 的只读工具。
    # 不承诺已发到外部服务的副作用可以撤销。
    task = runtime._internal_loop_task
    if task is not None and not task.done():
        task.cancel()
        done, _ = await asyncio.wait({task}, timeout=2)
        if not done:
            return {'local_cancelled': False, 'remote_status': '取消已请求，旧输出仍被冻结'}
    while not runtime._internal_turn_queue.empty():
        runtime._internal_turn_queue.get_nowait()
    runtime._internal_loop_task = None
    runtime._ensure_background_tasks_running()
    return {'local_cancelled': True, 'remote_status': '远端已接受的操作不能保证撤回'}


async def capability(plugin_id: str, capability_name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    del capability_name
    if plugin_id != 'heart.realtime':
        raise PermissionError('仅 Heart 实时插件可调用')
    session_id = str(args.get('session_id') or '')
    visitor = str(args.get('visitor_id') or '')
    connection = str(args.get('connection_id') or '')
    session = resolve_session(session_id, visitor)
    action = args.get('action')
    if action == 'bind':
        old = LEASES.get(session_id)
        if old and old.connection != connection:
            raise PermissionError('会话已被另一个连接绑定')
        if not connection or len(connection) > 128:
            raise ValueError('连接编号无效')
        LEASES.setdefault(session_id, Lease(visitor, connection))
        from src.config.config import global_config

        names=[global_config.bot.nickname,*global_config.bot.alias_names]
        return {'success': True, 'session_id': session_id, 'api_version':1,
                'character_names':[x.strip() for x in names if x.strip()][:16]}
    lease = LEASES.get(session_id)
    if lease is None or lease.visitor != visitor or lease.connection != connection:
        raise PermissionError('实时连接没有绑定原生会话')
    lease.touched = time.monotonic()
    if action == 'mode':
        if type(args.get('preemptive')) is not bool:
            raise ValueError('提前生成开关必须为布尔值')
        from src.services.heart_realtime_drafts import audit, cancel_prepared

        lease.preemptive = args['preemptive']
        if not lease.preemptive:
            await cancel_prepared(session_id, lease, '用户关闭提前生成')
        audit(session_id, lease, 'mode', output='提前生成已开启' if lease.preemptive else '提前生成已关闭')
        return {'success': True, 'preemptive': lease.preemptive}
    if action == 'speculate':
        from src.services.heart_realtime_drafts import prepare

        if lease.speculation_count >= 1:
            raise ValueError('本轮提前生成次数已用完')
        lease.speculation_count += 1
        lease.utterance = str(args['utterance_id'])
        return await prepare(session, lease, args)
    if action == 'spec_cancel':
        from src.services.heart_realtime_drafts import cancel_prepared

        await cancel_prepared(session_id, lease, str(args.get('reason', '输入变化')))
        return {'success': True}
    if action == 'approve_draft':
        from src.services.heart_realtime_drafts import normalize

        if args.get('epoch') != lease.epoch:
            raise ValueError('过期的草稿授权')
        candidate = lease.prepared
        approved = bool(candidate and lease.preemptive and normalize(candidate.text) == normalize(args['text'])
                        and candidate.evidence_key == lease.evidence_key and not args.get('query_pending'))
        if candidate:
            candidate.approved = approved
        lease.utterance = str(args['utterance_id'])
        return {'success': True, 'approved': approved}
    if action == 'native_bound':
        if args.get('epoch') != lease.epoch:
            raise ValueError('过期的正式消息绑定')
        lease.native_id = str(args['message_id'])
        return {'success': True}
    if action == 'evidence':
        if args.get('epoch') != lease.epoch:
            raise ValueError('过期的查询结果')
        key = str(args.get('evidence_key', ''))
        reply_ready_stale = lease.reply_evidence_key is not None and lease.reply_evidence_key != key
        if key != lease.evidence_key:
            previous = lease.evidence_changed
            lease.evidence, lease.evidence_key = args.get('result'), key
            lease.evidence_changed = asyncio.Event()
            previous.set()
        return {'success': True, 'reply_ready_stale': reply_ready_stale}
    if action == 'heartbeat':
        return {'success': True, 'epoch': lease.epoch}
    if action == 'begin':
        epoch = args.get('epoch')
        if not isinstance(epoch, int) or isinstance(epoch, bool) or epoch <= lease.epoch:
            raise ValueError('输出世代必须递增')
        from src.services.heart_realtime_drafts import cancel_prepared

        await cancel_prepared(session_id, lease, '插话或新一轮输入')
        lease.epoch, lease.active = epoch, False
        lease.utterance, lease.native_id, lease.speculation_count = '', '', 0
        lease.evidence_changed.set()
        lease.evidence, lease.evidence_key = None, ''
        lease.reply_evidence_key = None
        lease.evidence_changed = asyncio.Event()
        lease.drafts.clear()
        lease.committed.clear()
        lease.reservations.clear()
        result = await cancel_runtime(session_id)
        lease.cancel_pending = not result['local_cancelled']
        return {'success': True, **result}
    if action == 'activate':
        if args.get('epoch') != lease.epoch:
            raise ValueError('输入已被新一轮覆盖')
        if lease.cancel_pending:
            raise RuntimeError('旧推理未结束，必须再次停止并确认后才接受新输出')
        lease.active = True
        return {'success': True}
    if action == 'correct_generate':
        if args.get('epoch') != lease.epoch or not lease.active:
            raise ValueError('更正任务已失效')
        from src.chat.heart_flow.heartflow_manager import heartflow_manager

        runtime = await heartflow_manager.get_or_create_heartflow_chat(session_id)
        result = await runtime.enqueue_proactive_task(plugin_id=plugin_id,
            intent=str(args.get('intent') or '')[:6000], reason='核实结果推翻正在表达的同一事实',
            priority='high', metadata={'heart_realtime_epoch':lease.epoch})
        return {'success': True, **result}
    if action == 'read':
        from src.person_info.person_info import get_person_id
        from src.services.heart_memory_backend import NativeBackend

        from heart_shared.storage import AuditStore
        store = AuditStore()
        from heart_shared.live2d_logs import members
        with store.connect() as db:
            _, owned_sessions = members(db, session_id)
            owners = [r[0] for member in owned_sessions for r in db.execute(
                "SELECT user_id FROM session_sources WHERE session=? AND channel='Live2D' AND platform='webui'", (member,))]
        owners = list(dict.fromkeys([session.user_id, *owners]))
        gathered = await asyncio.gather(*(NativeBackend().person_facts(get_person_id(session.platform, owner)) for owner in owners))
        facts = list({fact['hash']: fact for group in gathered for fact in group}.values())
        query = str(args.get('query') or '')[:512]
        words = set(re.findall(r'[\u4e00-\u9fff]|[A-Za-z0-9]+', query.lower()))
        ranked = sorted(facts, key=lambda x: len(words & set(re.findall(
            r'[\u4e00-\u9fff]|[A-Za-z0-9]+', x['content'].lower()))), reverse=True)[:5]
        body = json.dumps(ranked, ensure_ascii=False, sort_keys=True)
        return {'success': True, 'items': ranked, 'source': '原生本人记忆',
                'version': hashlib.sha256(body.encode()).hexdigest()}
    if action == 'weather':
        if args.get('epoch') != lease.epoch:
            raise ValueError('已过期的天气查询')
        from src.core.tooling import ToolInvocation, ToolExecutionContext
        from src.plugin_runtime.component_query import component_query_service
        invocation = ToolInvocation(tool_name='query_weather', session_id=session_id,
            arguments={k: args[k] for k in ('city','days','period') if k in args},
            reasoning='用户在实时输入中明确要求查询天气')
        context = ToolExecutionContext(session_id=session_id, stream_id=session_id,
            user_id=session.user_id, platform=session.platform, is_group_chat=False)
        result = await component_query_service.invoke_tool_as_tool(invocation, context)
        body = result.structured_content if isinstance(result.structured_content, dict) else {
            'success':result.success, 'content':result.content or result.error_message,
            'error':result.error_message}
        return {'success':True, 'weather':body}
    if action == 'context':
        from src.chat.heart_flow.heartflow_manager import heartflow_manager
        from src.maisaka.context.messages import SessionBackedMessage
        from src.plugin_runtime.host.message_utils import PluginMessageUtils
        from datetime import datetime

        if args.get('epoch') != lease.epoch:
            raise ValueError('已过期的工具结果')
        text = str(args.get('text') or '')[:6000]
        runtime = await heartflow_manager.get_or_create_heartflow_chat(session_id)
        runtime._chat_history[:] = [x for x in runtime._chat_history
            if not (isinstance(x, SessionBackedMessage) and x.source_kind == 'heart_realtime_observation')]
        if text:
            runtime._chat_history.append(SessionBackedMessage(
                raw_message=PluginMessageUtils._message_sequence_from_dict([{'type':'text','data':text}]),
                visible_text=text, timestamp=datetime.now(), source_kind='heart_realtime_observation'))
        return {'success': True}
    if action == 'draft':
        text = str(args.get('text') or '')
        key = str(args.get('delivery_key') or '')
        if key and not re.fullmatch(r'[0-9a-f]{32}',key):
            raise ValueError('输出登记请求编号无效')
        reserved = lease.reservations.get(key)
        found = []
        for message_id, (epoch, message) in lease.drafts.items():
            body = speech_text(message)
            if epoch == lease.epoch and body == text.strip():
                if reserved == message_id:
                    return {'success':True,'draft_id':message_id,'text':body}
                if message_id not in lease.reservations.values():
                    found.append((message_id,body))
        if found:
            if not key and len(found)>1:
                raise ValueError('存在相同正文的多个草稿，需要本次输出登记请求编号')
            message_id,body=found[0]
            if key:lease.reservations[key]=message_id
            return {'success':True,'draft_id':message_id,'text':body}
        return {'success': True, 'pending': True, 'reason': '正文尚未登记为本轮草稿'}
    if action == 'commit':
        message_id = str(args.get('draft_id') or '')
        heard = str(args.get('text') or '')
        if args.get('epoch') != lease.epoch:
            raise ValueError('旧世代的播放回执')
        if message_id in lease.committed and lease.committed[message_id] == heard:
            return {'success': True, 'duplicate': True}
        entry = lease.drafts.get(message_id)
        if entry is None or entry[0] != args.get('epoch'):
            raise ValueError('旧输出或未知草稿的回执')
        original = speech_text(entry[1])
        previous = lease.committed.get(message_id, '')
        if not heard or not original.startswith(heard) or not heard.startswith(previous):
            raise ValueError('播放回执正文与草稿前缀不一致')
        if heard != original and not re.search(r'[。！？!?；;][”’」』）】]*\s*$', heard):
            return {'success': True, 'pending': True, 'reason': '等待完整句子的播放回执'}
        from src.plugin_runtime.host.message_utils import PluginMessageUtils
        from src.services.send_service import _store_sent_message, _notify_memory_automation_on_message_sent
        from src.services.send_service import _sync_sent_message_to_maisaka_history

        message = deepcopy(entry[1])
        delta = heard[len(previous):]
        if not delta:
            return {'success': True, 'duplicate': True}
        message.message_id = 'rt-heard-' + uuid.uuid5(uuid.NAMESPACE_URL,
            session_id + ':' + message_id + ':' + str(len(heard))).hex
        message.raw_message = PluginMessageUtils._message_sequence_from_dict([{'type':'text','data':delta}])
        message.processed_plain_text = delta
        message.display_message = delta
        # 存储先成功才记去重；草稿不经此入口就不会进入正式对话/自动记忆。
        await _store_sent_message(message)
        lease.committed[message_id] = heard
        _sync_sent_message_to_maisaka_history(message, source_kind='heart_realtime_heard')
        await _notify_memory_automation_on_message_sent(message)
        return {'success': True, 'heard_text': delta}
    if action == 'close':
        from src.services.heart_realtime_drafts import cancel_prepared

        await cancel_prepared(session_id, lease, '关闭插件或连接断开')
        lease.active = False
        await cancel_runtime(session_id)
        LEASES.pop(session_id, None)
        return {'success': True}
    raise ValueError('未知实时宿主操作')
