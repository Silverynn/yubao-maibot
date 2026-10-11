"""实时插件专用的原生 Replyer 草稿缓存。没有绑定的会话直接调用原模型。"""
from dataclasses import dataclass
from typing import Any, Dict, Optional

import asyncio
import copy
import hashlib
import json
import re
import time


@dataclass
class PreparedReply:
    epoch: int
    utterance: str
    text: str
    evidence_key: str
    task: Optional[asyncio.Task] = None
    result: Any = None
    signature: str = ''
    task_name: str = 'replyer'
    model_name: Optional[str] = None
    keyword_prompt: str = ''
    temporary_style: str = ''
    approved: bool = False
    consumed: bool = False


def normalize(text: str) -> str:
    return re.sub(r'\s+', ' ', str(text)).strip().rstrip('。！？!?，,；;')


def audit(session: str, lease: Any, phase: str, **data) -> None:
    from heart_shared.storage import AuditStore

    AuditStore().append('实时对话插件', session, plugin='heart.realtime', feature=data.pop('feature', '提前生成'),
        phase=phase, plugin_enabled=True, session_enabled=True,
        preemptive_enabled=lease.preemptive, epoch=lease.epoch,
        utterance_id=lease.utterance, message_id=lease.native_id, **data)


def history_before_target(history, target_id=''):
    from src.maisaka.context.messages import SessionBackedMessage

    return [x for x in history if not (isinstance(x, SessionBackedMessage) and
        (x.source_kind == 'heart_realtime_observation' or (target_id and x.message_id == target_id)))]


def signature(generator, history, requirements: str, session: str) -> str:
    from src.llm_models.payload_content.context_item import get_item_text

    items = generator._build_history_messages(history, False)
    material = [generator._build_system_prompt(None, '', stream_id=session), requirements,
                [(type(x).__name__, get_item_text(x)) for x in items]]
    return hashlib.sha256(json.dumps(material, ensure_ascii=False).encode()).hexdigest()


def with_evidence(items, evidence):
    if evidence is None:
        return items
    from src.llm_models.payload_content.context_item import ContextItemBuilder, RoleType

    from heart_shared.realtime_evidence import evidence_note
    note = evidence_note(evidence)
    return [*items, ContextItemBuilder().set_role(RoleType.System).add_text_content(note).build()]


def audit_evidence(session, lease, evidence, mode, key):
    from heart_shared.realtime_evidence import evidence_summary
    audit(session, lease, 'context_built', feature='回复查询依据',
        evidence_key=key, generation_mode=mode,
        summary=evidence_summary(evidence),
        status='已构建回复模型请求上下文；是否发送成功以模型生成记录为准')


def temporary_style(items) -> str:
    from src.llm_models.payload_content.context_item import get_item_text

    return '\n'.join(get_item_text(x) for x in items
                     if get_item_text(x).startswith('你的说话风格可以尝试：\n'))


async def cancel_prepared(session: str, lease: Any, reason: str) -> None:
    candidate = lease.prepared
    if candidate:
        candidate.approved = False
        if candidate.task and not candidate.task.done():
            candidate.task.cancel()
            await asyncio.gather(candidate.task, return_exceptions=True)
        audit(session, lease, 'discarded', reason=reason, output=candidate.result.response if candidate.result else '',
              remote_cancel_guaranteed=False)
        lease.prepared = None


async def prepare(session: Any, lease: Any, args: Dict[str, Any]):
    from src.chat.heart_flow.heartflow_manager import heartflow_manager
    from src.chat.replyer.replyer_manager import replyer_manager
    from src.common.data_models.llm_service_data_models import LLMGenerationOptions
    from src.llm_models.payload_content.context_item import ContextItemBuilder, RoleType

    if not lease.preemptive or args.get('epoch') != lease.epoch:
        raise ValueError('提前生成已关闭或输入已过期')
    if lease.prepared is not None:
        raise ValueError('每轮只允许一次提前生成')
    text = str(args['text'])[:6000]
    candidate = PreparedReply(lease.epoch, str(args['utterance_id']), text, str(args.get('evidence_key', '')))
    lease.prepared = candidate
    runtime = await heartflow_manager.get_or_create_heartflow_chat(session.session_id)
    generator = replyer_manager.get_replyer(chat_stream=session, request_type='maisaka.replyer', replyer_type='maisaka')
    if generator is None:
        raise RuntimeError('原生回复器不可用')
    history = history_before_target(list(runtime._chat_history))
    # 只读现有风格 Hook；不提交临时用户消息，不调用规划器或工具，不写心情/记忆。
    hook = await generator._get_runtime_manager().invoke_hook('maisaka.replyer.before_request',
        session_id=session.session_id, request_type='maisaka.replyer', task_name='replyer',
        model_name='', extra_prompt='', attempt=1, retry_count=0, max_retries=0,
        reply_message_id='', reply_reason='', selected_expression_ids=[], reply_tool_args={})
    if candidate is not lease.prepared or candidate.epoch != lease.epoch:
        raise asyncio.CancelledError()
    requirements = generator._build_reply_requirements(str(hook.kwargs.get('extra_prompt') or ''), [])
    candidate.signature = signature(generator, history, requirements, session.session_id)
    candidate.keyword_prompt = generator._build_keyword_reaction_prompt(chat_history=history, reply_message=None)
    candidate.task_name = str(hook.kwargs.get('task_name') or 'replyer')
    candidate.model_name = str(hook.kwargs.get('model_name') or '') or None
    model = generator.express_model if candidate.task_name == 'replyer' else generator._llm_client_cls(
        task_name=candidate.task_name, request_type='maisaka.replyer', session_id=session.session_id)
    evidence = args.get('evidence')
    started = time.monotonic()

    async def context_factory(client, model_info=None):
        items = generator._build_request_messages(chat_history=history, reply_message=None,
            reply_reason='', reply_requirements=requirements, stream_id=session.session_id,
            enable_visual_message=False, reply_tool_args={})
        candidate.temporary_style = temporary_style(items)
        items.append(ContextItemBuilder().set_role(RoleType.User).add_text_content(text).build())
        result = with_evidence(items, evidence)
        audit_evidence(session.session_id, lease, evidence, '提前草稿', candidate.evidence_key)
        return result

    async def generate():
        audit(session.session_id, lease, 'started', input=text, temporary=True,
              evidence_key=candidate.evidence_key, context_signature=candidate.signature,
              context_count=len(history), model_task=candidate.task_name)
        try:
            result = await asyncio.wait_for(model.generate_response_with_context(context_factory=context_factory,
                options=LLMGenerationOptions(model_name=candidate.model_name)), timeout=25)
            if candidate is not lease.prepared or candidate.epoch != lease.epoch:
                return None
            if not result.response.strip() or result.tool_calls:
                raise RuntimeError('草稿为空或包含工具调用，不能提前采用')
            candidate.result = result
            audit(session.session_id, lease, 'ready', output=result.response, model=result.model_name,
                usage={'input_count':result.prompt_tokens, 'output_count':result.completion_tokens,
                       'total_count':result.total_tokens}, elapsed_ms=round((time.monotonic()-started)*1000, 1))
            return {'success': True, 'output': result.response, 'evidence_key': candidate.evidence_key,
                    'model': result.model_name, 'total_tokens': result.total_tokens}
        except asyncio.CancelledError:
            audit(session.session_id, lease, 'cancelled', reason='输入、开关或证据已变化', remote_cancel_guaranteed=False)
            raise
        except Exception as error:
            audit(session.session_id, lease, 'failed', reason=type(error).__name__)
            candidate.approved = False
            raise

    candidate.task = asyncio.create_task(generate())
    return await candidate.task


async def generate_reply(*, session_id, generator, model, context_factory, options, reply_message,
                         requirements, tool_args, task_name, model_name, retry_count, expected_items=None,
                         reply_reason=''):
    """这是原生 Replyer 唯一接缝；规划、Hook、后处理、发送仍由原链路负责。"""
    from src.services.heart_realtime import LEASES

    lease = LEASES.get(session_id)
    if lease is None or not lease.active:
        return await model.generate_response_with_context(context_factory=context_factory, options=options)
    epoch = lease.epoch
    candidate = lease.prepared
    reason = ''
    if candidate and candidate.approved and not candidate.consumed and retry_count == 0:
        if reply_message is None or str(reply_message.message_id) != lease.native_id:
            reason = '不是本轮正式消息'
        elif normalize(reply_message.processed_plain_text) != normalize(candidate.text):
            reason = '正式转写与临时输入不同'
        elif candidate.evidence_key != lease.evidence_key:
            reason = '查询材料改变'
        elif any(value for key,value in tool_args.items() if not (key=='reply_style' and value=='正常回复')):
            reason = '原生规划器增加了回复条件或工具参考'
        elif reply_reason.strip():
            reason = '原生规划器增加了本轮思考参考'
        elif candidate.task_name != task_name or candidate.model_name != model_name:
            reason = '模型选择改变'
        elif expected_items is not None and temporary_style(expected_items) != candidate.temporary_style:
            reason = '原生临时说话风格改变'
        else:
            from src.chat.heart_flow.heartflow_manager import heartflow_manager
            runtime = heartflow_manager.heartflow_chat_list[session_id]
            history = history_before_target(list(runtime._chat_history), lease.native_id)
            if signature(generator, history, requirements, session_id) != candidate.signature:
                reason = '历史、人设或已确认心情改变'
            elif generator._build_keyword_reaction_prompt(chat_history=history, reply_message=reply_message) != candidate.keyword_prompt:
                reason = '正式输入触发了新的角色关键词规则'
        if not reason and candidate.task and not candidate.task.done():
            await asyncio.wait({candidate.task}, timeout=.8)
        if not reason and candidate.result and lease.active and epoch == lease.epoch:
            # 仍运行原生请求前 Item Hook，避免复用时绕开其他插件。
            actual_items = await context_factory(None, None)
            from src.llm_models.payload_content.context_item import get_item_text

            def item_signature(items):
                return [(type(x).__name__, re.sub(r'当前时间：\d{4}-\d\d-\d\d \d\d:\d\d:\d\d',
                    '当前时间：<本轮>',get_item_text(x))) for x in items]
            hook_unchanged = expected_items is None or item_signature(actual_items)==item_signature(expected_items)
            if hook_unchanged and candidate.evidence_key == lease.evidence_key:
                candidate.consumed = True
                lease.reply_evidence_key = candidate.evidence_key
                audit(session_id, lease, 'adopted', output=candidate.result.response,
                      saved_replyer_request=True, evidence_key=candidate.evidence_key)
                reused = copy.copy(candidate.result)
                # 真实用量已记录在草稿请求；复用没有再次请求，不把同一用量加两遍。
                reused.prompt_tokens = reused.completion_tokens = reused.total_tokens = 0
                reused.prompt_cache_hit_tokens = reused.prompt_cache_miss_tokens = 0
                reused.generation_attempts = ()
                return reused
            reason = '其他插件改写了本轮请求或新证据到达'
        reason = reason or '草稿未及时就绪或新证据到达'
    if candidate and not candidate.consumed:
        await cancel_prepared(session_id, lease, reason or '未批准复用')

    # 迟到的首次结果也可以使正在生成的旧稿失效；不只监测事实的第二次变化。
    # 每次 Replyer 调用最多因证据重生成一次，原生 Hook 自己的重试另行记录。
    for attempt in range(2):
        if not lease.active or epoch != lease.epoch:
            raise asyncio.CancelledError()
        key, evidence, changed = lease.evidence_key, lease.evidence, lease.evidence_changed
        lease.reply_evidence_key = None

        async def fresh_context(client, model_info=None):
            items = with_evidence(await context_factory(client, model_info), evidence)
            audit_evidence(session_id, lease, evidence, '正式回复', key)
            return items

        generation = asyncio.create_task(model.generate_response_with_context(context_factory=fresh_context, options=options))
        watcher = asyncio.create_task(changed.wait())
        try:
            done, _ = await asyncio.wait({generation, watcher}, return_when=asyncio.FIRST_COMPLETED)
            if epoch != lease.epoch or not lease.active:
                raise asyncio.CancelledError()
            if key != lease.evidence_key and attempt == 0:
                generation.cancel()
                await asyncio.gather(generation, return_exceptions=True)
                audit(session_id, lease, 'regenerated', reason='晚到查询改变本轮材料',
                      previous_evidence_key=key, evidence_key=lease.evidence_key,
                      remote_cancel_guaranteed=False)
                continue
            if key != lease.evidence_key:
                audit(session_id, lease, 'budget_exhausted', reason='证据再次变化，拒绝发送过期稿')
                raise RuntimeError('本轮证据再次变化，重生成预算已耗尽；请重新提问')
            result = await generation
            if key != lease.evidence_key:
                audit(session_id, lease, 'budget_exhausted', reason='生成结束时证据已过期')
                raise RuntimeError('本轮生成依据已过期，不能发送旧稿')
            audit(session_id, lease, 'generated', output=result.response, evidence_key=key,
                  evidence_stale=key != lease.evidence_key, generation_attempt=attempt+1,
                  model=result.model_name, usage={'input_count':result.prompt_tokens,
                  'output_count':result.completion_tokens, 'total_count':result.total_tokens})
            lease.reply_evidence_key = key
            return result
        finally:
            for task in (generation, watcher):
                if not task.done():
                    task.cancel()
            await asyncio.gather(generation, watcher, return_exceptions=True)
