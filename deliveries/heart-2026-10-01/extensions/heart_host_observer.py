"""记忆服务观察桥；个人事实写入前另接可关闭的冲突保护，不改 A_memorix 实现。"""

import asyncio
import logging
import time
import uuid
from functools import wraps

from heart_shared.memory_scope import filter_result


async def read_override(component, args):
    from src.services.heart_memory_scope import prepare_read
    return await prepare_read(component, args)


async def read_stored(component, args, result):
    from src.services.heart_memory_scope import stored_contents
    return await stored_contents(component, args, result)


async def notify(event):
    try:
        from heart_shared.memory_events import record
        from src.services.heart_memory_backend import settings
        if settings()['plugin_enabled']:
            await asyncio.to_thread(record, audit_store(), event)
    except Exception:
        # 审计失败必须可见，但绝不把已经成功的存储改成失败，也不重试记忆操作。
        logging.getLogger(__name__).exception("Heart记忆审计通知失败；原记忆结果未改变")


_audit_store = None


def audit_store():
    global _audit_store
    if _audit_store is None:
        from heart_shared.storage import AuditStore
        _audit_store = AuditStore()
    return _audit_store


def observed_memory_call(method):
    @wraps(method)
    async def wrapped(self, component_name, args=None, **kwargs):
        started = time.perf_counter()
        event = {"event_id": uuid.uuid4().hex, "component": component_name,
                 "arguments": args or {}, "result": None, "error": ""}
        try:
            call_args = dict(args or {})
            call_args.pop('_heart_source_message_ids', None)  # 仅审计用，不能传进原生算法。
            if component_name == 'search_memory' and call_args.get('chat_id'):
                from src.services.heart_memory_scope import constrain_search
                call_args = constrain_search(call_args)
            # 写入保护失败时不能绕过确认；其他调用仍保持原行为。
            result = (await read_override(component_name, call_args)
                      if component_name in {'get_person_profile', 'memory_profile_admin', 'heart_scoped_list', 'search_memory'}
                      and call_args.get('chat_id') else None)
            if component_name == "ingest_summary" or (component_name == "ingest_text" and (args or {}).get("source_type") == "person_fact"):
                from src.services.heart_memory_backend import before_memory_write
                result = await before_memory_write(component_name, args or {})
            if result is None:
                result = await method(self, component_name, call_args, **kwargs)
            if component_name == 'search_memory' and call_args.get('chat_id'):
                result = filter_result(result, call_args['chat_id'], str(call_args.get('person_id') or ''))
            event["result"] = result
            try:
                contents = (await read_stored(component_name, call_args, result)
                            if component_name in {'ingest_text', 'ingest_summary'} else [])
                if contents:
                    event['result'] = dict(result, _heart_stored_contents=contents)
            except Exception:
                logging.getLogger(__name__).exception('记忆已返回，但存储正文核对失败；不重试写入')
            return result
        except asyncio.CancelledError:
            # 取消可能发生在存储之后；不宣称失败、也不自动重试。
            logging.getLogger(__name__).warning("记忆调用被取消，结果未知：%s", component_name)
            event['error'] = '操作被取消，实际写入状态未知；不会自动重试'
            raise
        except Exception as exc:
            event["error"] = type(exc).__name__ + ": " + str(exc)
            raise
        finally:
            event["duration_ms"] = round((time.perf_counter() - started) * 1000, 2)
            # 不记录纯统计查询，防止打开后台产生大量无关内容。
            if component_name != "memory_stats" and (event["result"] is not None or event["error"]):
                await notify(event)
    return wrapped


def register_heart_hook_specs(registry):
    from src.plugin_runtime.host.hook_spec_registry import HookSpec
    return registry.register_hook_specs([HookSpec(
        name="heart.memory.after_operation", description="记忆服务调用完成后的只读事件",
        parameters_schema={"type": "object", "properties": {"event": {"type": "object"}}, "required": ["event"]},
        default_timeout_ms=2500, allow_abort=False, allow_kwargs_mutation=False)])
