"""记忆服务观察桥；个人事实写入前另接可关闭的冲突保护，不改 A_memorix 实现。"""

import asyncio
import logging
import time
import uuid
from functools import wraps


async def notify(event):
    try:
        from src.plugin_runtime.integration import get_plugin_runtime_manager
        await asyncio.wait_for(get_plugin_runtime_manager().invoke_hook(
            "heart.memory.after_operation", event=event), timeout=3)
    except Exception:
        # 审计失败必须可见，但绝不把已经成功的存储改成失败，也不重试记忆操作。
        logging.getLogger(__name__).exception("Heart记忆审计通知失败；原记忆结果未改变")


def observed_memory_call(method):
    @wraps(method)
    async def wrapped(self, component_name, args=None, **kwargs):
        started = time.perf_counter()
        event = {"event_id": uuid.uuid4().hex, "component": component_name,
                 "arguments": args or {}, "result": None, "error": ""}
        try:
            # 写入保护失败时不能绕过确认；其他调用仍保持原行为。
            result = None
            if component_name == "ingest_summary" or (component_name == "ingest_text" and (args or {}).get("source_type") == "person_fact"):
                from src.services.heart_memory_backend import before_memory_write
                result = await before_memory_write(component_name, args or {})
            if result is None:
                result = await method(self, component_name, args, **kwargs)
            event["result"] = result
            return result
        except asyncio.CancelledError:
            # 取消可能发生在存储之后；不宣称失败、也不自动重试。
            logging.getLogger(__name__).warning("记忆调用被取消，结果未知：%s", component_name)
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
