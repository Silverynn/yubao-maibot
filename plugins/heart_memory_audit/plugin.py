"""记录原生记忆事件，并为宿主冲突保护提供配置与本人确认命令。"""

import asyncio

from heart_shared.storage import AuditStore
from heart_shared.forget import NATURAL_FORGET_PATTERN, NATURAL_RESOLVE_PATTERN
from maibot_sdk import Command, Field, HookHandler, MaiBotPlugin, PluginConfigBase
from maibot_sdk.types import HookMode, HookOrder


class Switch(PluginConfigBase):
    config_version: str = Field(default="1.0.0", description="配置版本")
    enabled: bool = Field(default=True, description="记录本地明文对话和记忆；请先取得测试参与者同意")


class Conflicts(PluginConfigBase):
    enabled: bool = Field(default=True, description="人物事实写入前检查冲突；额外调用模型，疑似冲突先私聊本人确认")
    candidate_limit: int = Field(default=15, ge=1, le=30, description="比较的旧记忆候选数量；不保证覆盖全部历史")
    timeout_seconds: int = Field(default=20, ge=5, le=60, description="冲突判断等待秒数；失败则暂缓本次写入")
    confirmation_minutes: int = Field(default=10, ge=1, le=60, description="私聊确认有效分钟数，超过不更新")


class AutoCandidates(PluginConfigBase):
    enabled: bool = Field(default=True, description="把原生AI自动提取的人物事实先放进候选区；本人私聊确认后才写入长期记忆")
    max_pending_per_person: int = Field(default=20, ge=1, le=50, description="每人的待确认候选上限，避免无限累积")
    selection_guidance: str = Field(default="只记对用户本人具有持续意义、将来可能帮助理解其需求的事实或偏好。普通寒暄、一次性安排、猜测、引用他人的话、口令和敏感凭据都不要提取。不确定时输出空数组，不要为填满候选区而提取。",
                                    description="WebUI可修改：追加给原生AI提取器的筛选原则；空白则仍遵守原生规则；不应要求保存第三方隐私或密码")


class Config(PluginConfigBase):
    plugin: Switch = Field(default_factory=Switch)
    conflicts: Conflicts = Field(default_factory=Conflicts)
    auto_candidates: AutoCandidates = Field(default_factory=AutoCandidates)


def text_parts(value):
    """只取文本片段，不保存图片、音频、隐藏思考或完整模型请求。"""
    if isinstance(value, dict):
        if value.get("type") == "text" and isinstance(value.get("text"), str):
            yield value["text"]
        else:
            for key in ("parts", "content", "output"):
                yield from text_parts(value.get(key))
    elif isinstance(value, list):
        for item in value:
            yield from text_parts(item)


def memory_summary(component, arguments, result, error):
    if error:
        return {"status": "调用失败", "error": error}
    if not isinstance(result, dict):
        return {"status": "返回格式无法确认", "result_type": type(result).__name__}
    if component in {"ingest_text", "ingest_summary"}:
        stored, skipped = result.get("stored_ids") or [], result.get("skipped_ids") or []
        detail = str(result.get("detail") or result.get("reason") or result.get("error") or "")
        if detail.startswith("已进入候选记忆"):
            status = "候选待确认，尚未写入长期记忆"
        elif "等待本人私聊确认" in detail:
            status = "冲突待确认，尚未更新长期记忆"
        elif result.get("success") is False or result.get("error"):
            status = "写入失败"
        elif stored:
            status = "原生记忆服务报告已存储（不承诺每个ID都是全新记忆）"
        elif skipped:
            status = "跳过/重复，未报告新存储ID"
        else:
            status = "未报告存储ID；不能认定新增成功"
        return {"status": status, "submitted_text": arguments.get("text", ""),
                "stored_ids": stored, "skipped_ids": skipped,
                "detail": detail}
    if component == "search_memory":
        hits = result.get("hits") or []
        return {"status": "检索失败" if result.get("success") is False or result.get("error") else "检索完成",
                "query": arguments.get("query", ""), "hits": hits,
                "note": "这些是检索返回候选；不等于模型采用或在回复中引用", "error": result.get("error", "")}
    return {"status": "原生服务返回（具体成功/跳过状态见结果）", "result": result}


class MemoryAudit(MaiBotPlugin):
    config_model = Config

    @Command("remember", description="本人明确要求写入长期记忆", pattern=r"^/记住\s+(?P<content>.{1,500})$")
    async def remember(self, stream_id: str = "", **kwargs):
        result = await self.ctx.call_capability("heart.memory.manual", session_id=stream_id)
        message = result.get("message", "手动记忆请求未完成，请查看日志。")
        await self.ctx.send.text(message, stream_id)
        return bool(result.get("success")), message, True

    @Command("my_memories", description="本人私聊查看长期人物事实", pattern=r"^/我的记忆(?:\s+\d+)?$")
    async def my_memories(self, stream_id: str = "", **kwargs):
        result = await self.ctx.call_capability("heart.memory.manage", session_id=stream_id)
        message = result.get("message", "查询未完成，请查看日志。")
        await self.ctx.send.text(message, stream_id)
        return bool(result.get("success")), message, True

    @Command("forget_memory", description="本人私聊提出删除长期记忆", pattern=r"^/(?:忘记|忘掉)\s+.{1,200}$")
    async def forget_memory(self, stream_id: str = "", **kwargs):
        result = await self.ctx.call_capability("heart.memory.manage", session_id=stream_id)
        message = result.get("message", "删除请求未完成，请查看日志。")
        await self.ctx.send.text(message, stream_id)
        return bool(result.get("success")), message, True

    @Command("resolve_forget", description="本人私聊确认或取消忘记", pattern=r"^/(?:确认|取消)忘记\s+\d+$")
    async def resolve_forget(self, stream_id: str = "", **kwargs):
        result = await self.ctx.call_capability("heart.memory.manage", session_id=stream_id)
        message = result.get("message", "删除确认未完成，请查看日志。")
        await self.ctx.send.text(message, stream_id)
        return bool(result.get("success")), message, True

    @Command("natural_forget_memory", description="本人自然语言请求忘记长期记忆，先定位再确认",
             pattern=NATURAL_FORGET_PATTERN)
    async def natural_forget_memory(self, stream_id: str = "", **kwargs):
        result = await self.ctx.call_capability("heart.memory.manage", session_id=stream_id)
        message = result.get("message", "没有改动长期记忆，请查看日志。")
        await self.ctx.send.text(message, stream_id)
        return bool(result.get("success")), message, True

    @Command("natural_resolve_forget", description="本人不用斜杠确认或取消忘记",
             pattern=NATURAL_RESOLVE_PATTERN)
    async def natural_resolve_forget(self, stream_id: str = "", **kwargs):
        result = await self.ctx.call_capability("heart.memory.manage", session_id=stream_id)
        message = result.get("message", "没有改动长期记忆，请查看日志。")
        await self.ctx.send.text(message, stream_id)
        return bool(result.get("success")), message, True

    @Command("memory_candidates", description="本人私聊查看候选记忆", pattern=r"^/?候选记忆$")
    async def memory_candidates(self, stream_id: str = "", **kwargs):
        result = await self.ctx.call_capability("heart.memory.candidates", session_id=stream_id)
        message = result.get("message", "候选记忆查询未完成，请查看日志。")
        await self.ctx.send.text(message, stream_id)
        return bool(result.get("success")), message, True

    @Command("resolve_candidate", description="本人私聊确认或忽略候选记忆",
             pattern=r"^/?(?:确认|忽略)候选记忆\s+\d+$")
    async def resolve_candidate(self, stream_id: str = "", **kwargs):
        result = await self.ctx.call_capability("heart.memory.candidates", session_id=stream_id)
        message = result.get("message", "候选记忆处理未完成，请查看日志。")
        await self.ctx.send.text(message, stream_id)
        return bool(result.get("success")), message, True

    @Command("confirm_memory", description="本人私聊确认或取消记忆更新", pattern=r"^/?(?:确认|取消)记忆更新\s+\d+$")
    async def confirm_memory(self, stream_id: str = "", **kwargs):
        result = await self.ctx.call_capability("heart.memory.resolve", session_id=stream_id)
        message = result.get("message", "未取得处理结果，请查看后台日志。")
        await self.ctx.send.text(message, stream_id)
        return True, message, True

    async def on_load(self):
        self.store = AuditStore()
        await asyncio.to_thread(self.store.rebuild)
        self._get_logger().info("Heart记忆记录已加载：%s；冲突保护需配套宿主接口", self.store.root)

    async def on_unload(self):
        """每次操作自行关闭连接，无常驻线程或后台任务需要终止。"""

    async def on_config_update(self, scope, config_data, version):
        """Runner 已注入新配置；每次 Hook 都读取当前 self.config。"""

    @HookHandler("chat.receive.after_process", mode=HookMode.BLOCKING, order=HookOrder.LATE)
    async def incoming(self, message=None, **kwargs):
        if self.config.plugin.enabled and isinstance(message, dict):
            await asyncio.to_thread(self.store.record_message, message)
        return {"action": "continue"}

    @HookHandler("heart.memory.after_operation", mode=HookMode.BLOCKING)
    async def memory(self, event=None, **kwargs):
        if not self.config.plugin.enabled or not isinstance(event, dict):
            return {"action": "continue"}
        args = event.get("arguments") or {}
        metadata = args.get("metadata") or {}
        await asyncio.to_thread(self.store.append, "记忆操作结果", str(args.get("chat_id") or args.get("session_id") or ""),
            event_id=event["event_id"], operation=event["component"],
            evidence_message_ids=metadata.get("evidence_message_ids", []),
            duration_ms=event["duration_ms"], source_type=args.get("source_type", ""),
            person_ids=args.get("person_ids", []), person_id=args.get("person_id", ""),
            action=args.get("action", ""), external_id=args.get("external_id", ""),
            outcome=memory_summary(event["component"], args, event.get("result"), event.get("error")))
        return {"action": "continue"}

    async def observe_prompt(self, stage, kwargs):
        if not self.config.plugin.enabled:
            return {"action": "continue"}
        markers = ("【长期记忆检索结果-内部参考】", "【人物画像-内部参考】")
        references = []
        for item in kwargs.get("items", []):
            for text in text_parts(item):
                for marker in markers:
                    if marker in text:
                        # marker 是当前固定上游的参考块标志；不是在读取模型的隐藏推理。
                        references.append({"marker": marker, "text": text[text.index(marker):]})
        await asyncio.to_thread(self.store.append, "模型请求中的记忆参考", str(kwargs.get("session_id") or ""),
            message_id=str(kwargs.get("reply_message_id") or ""), stage=stage,
            references=references, note="按固定版本参考标记识别；空列表不证明没有其他形式的上下文记忆。提供给模型不等于最终采用。")
        return {"action": "continue"}

    @HookHandler("maisaka.planner.before_request", mode=HookMode.BLOCKING, order=HookOrder.LATE)
    async def planner(self, **kwargs):
        return await self.observe_prompt("planner", kwargs)

    @HookHandler("maisaka.replyer.before_model_request", mode=HookMode.BLOCKING, order=HookOrder.LATE)
    async def reply_prompt(self, **kwargs):
        return await self.observe_prompt("replyer", kwargs)

    @HookHandler("maisaka.replyer.after_response", mode=HookMode.BLOCKING, order=HookOrder.LATE)
    async def reply(self, **kwargs):
        if self.config.plugin.enabled:
            await asyncio.to_thread(self.store.append, "生成回复（尚非送达）", str(kwargs.get("session_id") or ""),
                message_id=str(kwargs.get("reply_message_id") or ""), response=kwargs.get("response", ""))
        return {"action": "continue"}

    @HookHandler("send_service.after_send", mode=HookMode.BLOCKING)
    async def sent(self, message=None, sent=False, **kwargs):
        if self.config.plugin.enabled and isinstance(message, dict):
            await asyncio.to_thread(self.store.append, "发送结果", str(message.get("session_id") or ""),
                message_id=str(kwargs.get("reply_message_id") or ""), outgoing_id=message.get("message_id", ""),
                response=message.get("processed_plain_text", ""), sent=bool(sent), note="平台发送结果，不表示对方已读")
        return {"action": "continue"}


def create_plugin():
    return MemoryAudit()
