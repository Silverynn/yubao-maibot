"""在 MaiBot 根目录验证真实加载器/记忆入口，后端返回虚构数据，不联网。"""

import asyncio
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType


async def main():
    runtime = Path.cwd()
    sys.path.insert(0, str(runtime))
    from src.plugin_runtime.host.hook_spec_registry import HookSpecRegistry
    from src.plugin_runtime.runner.plugin_loader import PluginLoader
    from src.services import heart_host_observer as observer
    from src.services.heart_host_observer import register_heart_hook_specs
    # 导入真实 MemoryService，但替换底层模块，避免初始化原生数据库/配置/网络。
    backend_module = ModuleType("src.A_memorix.host_service")
    backend_module.a_memorix_host_service = None
    sys.modules["src.A_memorix.host_service"] = backend_module
    from heart_shared.storage import AuditStore
    from src.services import memory_service as native
    from src.services.heart_memory_backend import CONFIRMED_WRITE

    loader = PluginLoader()
    metas = {m.plugin_id: m for m in loader.discover_and_load([str(runtime / "plugins")])}
    assert "heart.memory-audit" in metas and "heart.mood" in metas, loader._failed_plugins
    registry = HookSpecRegistry()
    specs = register_heart_hook_specs(registry)
    assert not specs[0].allow_abort and not specs[0].allow_kwargs_mutation
    demo = runtime.parent / "heart-demo" / uuid.uuid4().hex
    store = AuditStore(demo)
    audit = metas["heart.memory-audit"].instance
    mood = metas["heart.mood"].instance
    for instance in (audit, mood):
        instance.set_plugin_config({})
        instance.store = store
    # 从已加载插件的模块取引擎，保证不是另一个测试替身。
    mood.engine = sys.modules[type(mood).__module__].MoodEngine(store)
    message = {"session_id": "synthetic-session", "message_id": "synthetic-message",
               "processed_plain_text": "谢谢你，我准备学习Python",
               "message_info": {"user_info": {"user_id": "synthetic-user", "user_nickname": "虚构同学"}}}
    await mood.incoming(message=message)
    await audit.incoming(message=message)

    class FakeBackend:
        async def invoke(self, component, args, **kwargs):
            if component == "search_memory":
                return {"success": True, "hits": [{"content": "准备学习Python", "hash": "synthetic-fact", "score": 0.85}]}
            if component == "ingest_text":
                return {"success": True, "stored_ids": ["synthetic-fact"]}
            raise TimeoutError("离线故障演示")

    old_backend, old_notify = native.a_memorix_host_service, observer.notify
    native.a_memorix_host_service = FakeBackend()
    async def deliver(event):
        await audit.memory(event=event)
    observer.notify = deliver
    try:
        # 此处只测试真实审计入口；冲突流程另有独立测试，禁止连接真实模型。
        token = CONFIRMED_WRITE.set(True)
        try:
            result = await native.memory_service.ingest_text(external_id="synthetic-source", source_type="person_fact",
                text="准备学习Python", chat_id="synthetic-session", metadata={"evidence_message_ids": ["synthetic-message"]})
        finally:
            CONFIRMED_WRITE.reset(token)
        assert result.success and result.stored_ids == ["synthetic-fact"]
        found = await native.memory_service.search("学习", chat_id="synthetic-session")
        assert found.success and found.hits[0].hash_value == "synthetic-fact"
        failed = await native.memory_service.ingest_summary(external_id="synthetic-failed", chat_id="synthetic-session", text="故障演示")
        assert not failed.success
    finally:
        native.a_memorix_host_service, observer.notify = old_backend, old_notify
    request = await mood.before_reply(session_id="synthetic-session", reply_message_id="synthetic-message", extra_prompt="原有要求")
    assert "55.0/100" in request["modified_kwargs"]["extra_prompt"]
    await audit.reply_prompt(session_id="synthetic-session", reply_message_id="synthetic-message", items=[
        {"item_type": "UserMessageItem", "parts": [{"type": "text", "text": "【长期记忆检索结果-内部参考】\n准备学习Python"}]}])
    await audit.reply(session_id="synthetic-session", reply_message_id="synthetic-message", response="虚构回复：今天先练习变量吧。")
    await audit.sent(message={**message, "message_id": "synthetic-reply", "processed_plain_text": "虚构回复"}, sent=True, reply_message_id="synthetic-message")
    with store.connect() as db:
        events = [json.loads(row[0]) for row in db.execute("SELECT payload FROM events")]
    assert any(e.get("outcome", {}).get("stored_ids") == ["synthetic-fact"] for e in events)
    assert any(e.get("outcome", {}).get("status") == "调用失败" for e in events)
    assert all(e["mood"].get("value") == 55 for e in events)
    print(f"PASS {datetime.now(timezone.utc).astimezone().isoformat()}：真实加载器/只读规格/真实MemoryService入口/统一日志，共{len(events)}条虚构事件。")
    print(f"示例日志（全部虚构）：{demo}")


if __name__ == "__main__":
    asyncio.run(main())
