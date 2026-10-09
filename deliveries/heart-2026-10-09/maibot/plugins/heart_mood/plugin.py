"""读取持久化心情，追加临时回复要求；不覆盖原有人设和历史。"""

import asyncio
import json
import time
from contextlib import suppress

from heart_shared.storage import AuditStore
from heart_shared.webui_labels import localize_schema
from maibot_sdk import Field, HookHandler, MaiBotPlugin, PluginConfigBase
from maibot_sdk.types import HookMode, HookOrder
from pydantic import model_validator

from .appraiser import AIAppraiser
from .engine import MoodEngine
from .emotions import apply_emotion, publish_view


class Switch(PluginConfigBase):
    config_version: str = Field(default="1.0.0", description="配置版本")
    enabled: bool = Field(default=True, description="启用心情状态和风格影响")


class Settings(PluginConfigBase):
    baseline: float = Field(default=50, ge=0, le=100, description="初始值与长期恢复目标")
    recovery_per_hour: float = Field(default=2, ge=0, le=100, description="每小时向基准恢复多少分，收到消息时结算")
    max_delta: float = Field(default=8, ge=0, le=20, description="单条对话刺激变化上限")
    cooldown_seconds: float = Field(default=60, ge=0, le=86400, description="同一用户重复刺激冷却时间")


class Rules(PluginConfigBase):
    gratitude_terms: list[str] = Field(default_factory=lambda: ["谢谢你", "谢谢麦麦", "你真棒", "你帮了我", "辛苦你了"], description="明确感谢/认可词组")
    gratitude_delta: float = Field(default=5, ge=0, le=20, description="感谢时的心情加分")
    insult_terms: list[str] = Field(default_factory=lambda: ["你真没用", "你是笨蛋", "你太差劲", "讨厌你"], description="直接针对机器人的贬低词组")
    insult_delta: float = Field(default=-6, ge=-20, le=0, description="被直接贬低时减分")
    distress_terms: list[str] = Field(default_factory=lambda: ["我很难过", "我好难过", "我很伤心", "我好孤独"], description="触发角色共情、语气收敛的词组")
    distress_delta: float = Field(default=-2, ge=-20, le=0, description="共情时略微降低活跃程度；不是判断用户心理")


class Prompts(PluginConfigBase):
    low_below: float = Field(default=35, ge=0, le=100, description="低于此数值使用 low 提示词")
    high_at_least: float = Field(default=65, ge=0, le=100, description="达到此数值使用 high 提示词")
    safety: str = Field(default="只调整表达风格，不改变身份、事实、记忆、工具权限和安全边界。不要主动报告数值，不要说用户欠你、责怪用户或索取安慰。用户遇到困难时优先认真共情；严肃问题不插科打诨。", description="所有心情档位共用的约束")
    low: str = Field(default="表达稍微安静、克制，句子可以短一些，减少感叹号和表情；仍耐心、尊重地回答，不冷暴力、不讽刺、不降低帮助质量。", description="偏低时的回复要求")
    neutral: str = Field(default="保持原有人设的自然、平和语气，清楚回答，不刻意夸张情绪。", description="平稳时的回复要求")
    high: str = Field(default="表达更轻快、温暖，可以适度鼓励或用少量表情；不要浮夸，不违背原有人设，不主动背诵记忆。", description="偏高时的回复要求")

    @model_validator(mode="after")
    def check_thresholds(self):
        if self.low_below >= self.high_at_least:
            raise ValueError("low_below 必须小于 high_at_least")
        return self


class EmotionState(PluginConfigBase):
    name: str = Field(default="新状态", min_length=1, max_length=30, description="状态名称")
    minimum: float = Field(default=0, ge=0, le=100, description="最低分（含）")
    maximum: float = Field(default=100, ge=0, le=100, description="最高分（不含，100除外）")
    style: str = Field(default="保持自然友好的语气。", min_length=1, max_length=2000, description="这个状态下的聊天风格提示词")


class States(PluginConfigBase):
    __ui_label__ = "自定义情绪状态"
    __ui_order__ = 0
    enabled: bool = Field(default=True, description="使用下方自定义区间；关闭则使用旧版三档设置")
    entries: list[EmotionState] = Field(default_factory=lambda: [
        EmotionState(name="低落", minimum=0, maximum=35, style="语气安静克制，但仍耐心帮助用户，不冷暴力。"),
        EmotionState(name="平静", minimum=35, maximum=65, style="平和自然，保持原有人设。"),
        EmotionState(name="开心", minimum=65, maximum=85, style="轻快温暖，适度鼓励，可用少量表情。"),
        EmotionState(name="兴奋", minimum=85, maximum=100, style="更有活力、分享喜悦，但不浮夸；严肃问题仍认真回答。"),
    ], description="情绪状态列表（可以新增、删除和编辑）", min_length=1, max_length=20)

    @model_validator(mode="after")
    def validate_intervals(self):
        if not self.enabled:
            return self
        ordered = sorted(self.entries, key=lambda item: item.minimum)
        if len({item.name.strip() for item in ordered}) != len(ordered):
            raise ValueError("情绪名称不能重复")
        boundary = 0
        for item in ordered:
            if not item.name.strip() or not item.style.strip() or item.minimum != boundary or item.maximum <= item.minimum:
                raise ValueError("情绪区间必须从0连续覆盖到100，不能重叠、留空或使用空名称/提示词")
            boundary = item.maximum
        if boundary != 100:
            raise ValueError("最后一个情绪区间必须到100")
        return self


class AISettings(PluginConfigBase):
    __ui_label__ = "AI心情判断（可选）"
    enabled: bool = Field(default=False, description="开启后AI判断优先，不与关键词重复加分；会额外调用所选模型")
    model_name: str = Field(default="", max_length=128, description="心情判断专用模型名称；留空使用utils任务的默认模型，填写时须与模型配置中的name完全一致")
    fallback_keywords: bool = Field(default=False, description="仅AI失败时改用关键词；关闭则失败不计刺激，仍记录原因")
    timeout_seconds: float = Field(default=20, ge=1, le=60, description="单次AI等待上限秒数；后台评估不阻塞聊天，旧配置的6秒会保留")
    max_output_tokens: int = Field(default=4096, ge=256, le=16384, description="模型输出长度上限（不是心情分数）；过小可能截断，过大会增加费用与等待")
    delta_multiplier: float = Field(default=1, ge=0, le=3, description="AI变化倍率：0不计刺激，0.5更温和，1原建议，2更敏感；之后仍执行各项上限和冷却")
    positive_limit: float = Field(default=8, ge=0, le=20, description="AI每条最多加多少分；最终也不能超过mood.max_delta")
    negative_limit: float = Field(default=8, ge=0, le=20, description="AI每条最多减多少分（填正数）；最终也不能超过mood.max_delta")
    confidence_threshold: float = Field(default=0.7, ge=0, le=1, description="低于此把握程度保持不变，不转用关键词")
    guidance: str = Field(default="评价角色受到的互动影响而不是给用户打分；克制波动，尊重用户，普通知识提问通常保持不变。", max_length=2000, description="可修改的AI判断补充要求；不是回复风格提示词")


class PerformanceSettings(PluginConfigBase):
    __ui_label__ = "响应速度与拥堵保护"
    reply_first: bool = Field(default=True, description="优先生成回复，不为本轮心情评估额外等待；评估完成后仍更新心情和表情。关闭则使用原reply_wait_seconds")
    failure_pause_seconds: float = Field(default=60, ge=0, le=120, description="连续两次模型调用失败后暂停新评估的秒数，减轻上游拥堵；暂停期间保留已确认情绪并记录跳过，不伪造AI结果。0关闭")
    group_batch_seconds: float = Field(default=10, ge=0, le=120, description="群聊最长收集等待秒数：从首条消息起计时，条数够了提前评估；0恢复逐条。不是模型完成时限")
    group_batch_max_messages: int = Field(default=8, ge=1, le=20, description="群聊累计几条用户消息评估一次；条数或最长等待任一先到就提交，同一会话独立累计")
    private_batch_max_messages: int = Field(default=1, ge=1, le=20, description="私聊和Live2D累计几条用户消息评估一次；默认1逐条，建议需减载时设3")
    private_batch_seconds: float = Field(default=15, ge=1, le=120, description="私聊和Live2D累计消息的最长等待秒数，避免只聊一句永远不评估；从首条消息起算")


class TopicEmotion(PluginConfigBase):
    label: str = Field(default="疑惑", min_length=1, max_length=20, description="情绪分类名称，与心情分数无固定对应")
    expression: str = Field(default="问号", min_length=1, max_length=40, description="模型中的准确表情名，例如问号、生气、开心兴奋")
    style: str = Field(default="自然表达困惑，说明不确定之处并尝试澄清，不假装已经懂了。", max_length=1500, description="此话题情绪下的回复风格，QQ和Live2D都生效")


class TopicEmotions(PluginConfigBase):
    __ui_label__ = "话题临时情绪（独立于分数）"
    enabled: bool = Field(default=False, description="启用AI话题情绪；QQ和Live2D均生效，与AI加减分共用一次utils调用。旧配置默认关闭")
    confidence_threshold: float = Field(default=.7, ge=0, le=1, description="产生/改变/结束情绪的最低把握；不足则保留原状态")
    reply_wait_seconds: float = Field(default=4, ge=0, le=8, description="生成回复前最多等待当前会话情绪评估的秒数；超时使用已确认状态并说明正在评估")
    allow_expression_requests: bool = Field(default=True, description="同时识别普通话中的表情表演请求，如笑一个；仅向Live2D发送短暂展示，不把表演当成真实情绪")
    expression_request_seconds: float = Field(default=8, ge=3, le=30, description="自然语言请求的表情展示秒数；不影响话题情绪的生命周期")
    expression_request_guidance: str = Field(default="只有用户直接要求角色做表情、笑一个、演示或恢复自动时才执行。否定、转述、假设和询问情绪原因不等于表演请求。只表演不代表角色真的产生该情绪。", max_length=1500, description="自然语言表情请求的判断提示词")
    guidance: str = Field(default="话题未结束时延续情绪；明确说解决了、告别该话题、转入不相关新话题时才结束。群聊其他成员插话不等于结束。询问你当前情绪是状态查询，不清空状态。疑惑不等于低心情，生气不等于悲伤；只评价角色自身反应，不把用户情绪直接复制给角色。", max_length=2500, description="可编辑的情绪与话题判断规则；没有固定秒数过期")
    entries: list[TopicEmotion] = Field(default_factory=lambda: [
        TopicEmotion(),
        TopicEmotion(label="开心", expression="开心兴奋", style="分享喜悦，轻快自然。"),
        TopicEmotion(label="生气", expression="生气", style="可以坚定表达不赞同，但不辱骂、不报复、不降低帮助质量。"),
        TopicEmotion(label="难过", expression="悲伤", style="温和安静地表达遗憾，仍认真帮助。"),
        TopicEmotion(label="惊讶", expression="感叹号", style="适度表达惊讶，之后继续认真讨论。"),
        TopicEmotion(label="害羞", expression="脸红", style="略微腼腆，不强行制造亲密关系。"),
    ], min_length=1, max_length=20, description="可以添加分类、修改模型表情名和风格提示词")

    @model_validator(mode="after")
    def unique_labels(self):
        if len({e.label.strip() for e in self.entries}) != len(self.entries) or any(not e.label.strip() or not e.expression.strip() for e in self.entries):
            raise ValueError("情绪名称必须非空且不重复，表情名称不能为空")
        return self


class AvatarBand(PluginConfigBase):
    state: str = Field(default="平静", min_length=1, max_length=30, description="对应自定义心情区间的名称")
    expression: str = Field(default="平静", min_length=1, max_length=40, description="没有临时情绪时持续显示的模型表情")


class Avatar(PluginConfigBase):
    __ui_label__ = "Live2D常态表情"
    entries: list[AvatarBand] = Field(default_factory=lambda: [
        AvatarBand(state="低落", expression="悲伤"), AvatarBand(),
        AvatarBand(state="开心", expression="星星眼"), AvatarBand(state="兴奋", expression="开心兴奋"),
    ], max_length=20, description="按心情档位映射常态；未配置的档位使用平静。临时情绪结束后回到这里")


class Config(PluginConfigBase):
    plugin: Switch = Field(default_factory=Switch)
    mood: Settings = Field(default_factory=Settings)
    rules: Rules = Field(default_factory=Rules)
    prompts: Prompts = Field(default_factory=Prompts)
    states: States = Field(default_factory=States)
    ai: AISettings = Field(default_factory=AISettings)
    performance: PerformanceSettings = Field(default_factory=PerformanceSettings)
    emotions: TopicEmotions = Field(default_factory=TopicEmotions)
    avatar: Avatar = Field(default_factory=Avatar)


# 只翻译 WebUI 的分组与字段标题；英文键继续负责读取已有 config.toml。
WEBUI_LABELS = {
    "plugin": ("心情插件总开关", {
        "config_version": "配置版本（无需修改）", "enabled": "启用心情与回复风格",
    }),
    "mood": ("心情数值与恢复", {
        "baseline": "初始与恢复目标心情值", "recovery_per_hour": "每小时恢复分数",
        "max_delta": "每轮心情变化上限", "cooldown_seconds": "同一用户刺激冷却（秒）",
    }),
    "rules": ("关键词心情规则", {
        "gratitude_terms": "感谢与认可关键词", "gratitude_delta": "感谢时增加分数",
        "insult_terms": "直接贬低关键词", "insult_delta": "被贬低时变化分数",
        "distress_terms": "用户难过的关键词", "distress_delta": "共情时变化分数",
    }),
    "prompts": ("旧版三档回复风格", {
        "low_below": "低心情分界值", "high_at_least": "高心情分界值",
        "safety": "所有档位共用的安全要求", "low": "低心情回复风格",
        "neutral": "平稳心情回复风格", "high": "高心情回复风格",
    }),
    "states": ("自定义心情分段与风格", {
        "enabled": "启用自定义分段", "entries": "心情分段列表",
    }),
    "ai": ("AI 心情判断（可选）", {
        "enabled": "启用 AI 判断", "fallback_keywords": "AI 失败时使用关键词规则",
        "model_name": "心情判断专用模型名称（留空使用utils）",
        "timeout_seconds": "单次判断最长等待（秒）", "max_output_tokens": "模型输出长度上限",
        "delta_multiplier": "AI 建议变化倍率", "positive_limit": "单次最多加分",
        "negative_limit": "单次最多减分", "confidence_threshold": "最低判断把握程度",
        "guidance": "AI 心情判断补充提示词",
    }),
    "performance": ("响应速度与拥堵保护", {
        "reply_first": "先回复，再后台判断心情", "failure_pause_seconds": "连续失败后的暂停（秒）",
        "group_batch_seconds": "群聊最长收集时间（秒）", "group_batch_max_messages": "群聊每几条消息判断一次",
        "private_batch_max_messages": "私聊/Live2D 每几条判断一次", "private_batch_seconds": "私聊/Live2D 最长收集时间（秒）",
    }),
    "emotions": ("话题临时情绪（独立于分数）", {
        "enabled": "启用话题临时情绪", "confidence_threshold": "情绪判断最低把握程度",
        "reply_wait_seconds": "回复前最多等待判断（秒）", "allow_expression_requests": "允许自然语言请求表情",
        "expression_request_seconds": "请求表情展示时间（秒）",
        "expression_request_guidance": "表情请求识别提示词",
        "guidance": "话题情绪判断提示词", "entries": "临时情绪与表情列表",
    }),
    "avatar": ("Live2D 常态表情", {
        "entries": "心情分段对应的常态表情",
    }),
}


class MoodPlugin(MaiBotPlugin):
    config_model = Config

    @classmethod
    def build_config_schema(cls, **kwargs):
        """仅修改页面文字，不重置持久化的心情或用户配置。"""
        return localize_schema(super().build_config_schema(**kwargs), WEBUI_LABELS)

    async def on_load(self):
        self.store = AuditStore()
        await asyncio.to_thread(self.store.start_card_logs)
        await asyncio.to_thread(self.store.prune)
        self._retention_task = asyncio.create_task(self.store.retention_loop())
        self.engine = MoodEngine(self.store)
        self.appraiser = AIAppraiser(self)
        await asyncio.to_thread(self.sync_views)
        self._get_logger().info("Heart心情插件已加载；数值保存在：%s", self.store.root)

    async def on_unload(self):
        if hasattr(self, "_retention_task"):
            self._retention_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._retention_task
        if hasattr(self, "appraiser"):
            await self.appraiser.close()

    async def on_config_update(self, scope, config_data, version):
        """Runner 注入配置后下次请求生效；不重置已有心情数值。"""
        if hasattr(self, "store"):
            await asyncio.to_thread(self.sync_views)

    def sync_views(self):
        with self.store.transaction() as db:
            for row in db.execute("SELECT session FROM moods").fetchall():
                if not self.config.emotions.enabled:
                    apply_emotion(self.store, db, row[0], "", None, self.config.emotions)
                else:
                    state = self.store.emotion_snapshot(db, row[0])
                    if state.get("active") and state.get("label") not in {e.label for e in self.config.emotions.entries}:
                        apply_emotion(self.store, db, row[0], "", {"emotion": {"action": "clear", "reason": "管理员移除了该情绪分类"}}, self.config.emotions)
                publish_view(self.store, db, row[0], self.config)

    @HookHandler("chat.receive.after_process", mode=HookMode.BLOCKING, order=HookOrder.EARLY)
    async def incoming(self, message=None, **kwargs):
        if self.config.plugin.enabled and isinstance(message, dict):
            text = str(message.get("processed_plain_text") or "").strip()
            frequency = self.config.performance
            is_group = bool((message.get('message_info') or {}).get('group_info'))
            batch_enabled = (frequency.group_batch_max_messages > 1 and frequency.group_batch_seconds > 0
                             if is_group else frequency.private_batch_max_messages > 1 and frequency.private_batch_seconds > 0)
            if (self.config.ai.enabled or self.config.emotions.enabled or batch_enabled) and text and not text.startswith(("/", "!")) and not message.get("is_notify") and not message.get("is_command"):
                if not hasattr(self, "appraiser"):
                    self.appraiser = AIAppraiser(self)
                await self.appraiser.submit(message)
            else:
                await asyncio.to_thread(self.engine.update, message, self.config.mood, self.config.rules, config=self.config)
        return {"action": "continue"}

    @HookHandler("maisaka.replyer.before_request", mode=HookMode.BLOCKING, order=HookOrder.LATE)
    async def before_reply(self, **kwargs):
        if not self.config.plugin.enabled:
            return {"action": "continue"}
        session = str(kwargs.get("session_id") or "")
        pending = False
        wait_started = time.monotonic()
        if self.config.emotions.enabled and hasattr(self, "appraiser"):
            seconds = 0 if self.config.performance.reply_first else self.config.emotions.reply_wait_seconds
            pending = not await self.appraiser.wait_session(session, seconds)
        wait_ms = round((time.monotonic() - wait_started) * 1000)
        prompt, state, band = await asyncio.to_thread(self.engine.style, session, self.config.prompts, self.config.states)
        if self.config.emotions.enabled:
            with self.store.connect() as db:
                emotion = self.store.emotion_snapshot(db, session)
            if emotion.get("active"):
                selected = next((e for e in self.config.emotions.entries if e.label == emotion["label"]), None)
                prompt += (f"\n【已确认的话题情绪】{emotion['label']}，强度{emotion['intensity']:.0%}；话题：{emotion['topic']}。"
                           f"原因：{emotion['reason']}。\n{selected.style if selected else ''}\n心情分数和本情绪不是同一指标。")
            else:
                prompt += "\n【已确认的话题情绪】当前没有额外的临时情绪。"
            prompt += "\n如果用户问你现在的情绪，应依据上述已确认状态自然回答，不编造改变，不把用户情绪当自己的。"
            if pending:
                prompt += "\n本轮情绪评估还未完成，上面是最近已确认状态；不要声称本轮已经产生了新的情绪。"
            with self.store.connect() as db:
                row = db.execute('SELECT detail FROM avatar_views WHERE session=?', (session,)).fetchone()
                performance = json.loads(row[0]).get('performance', {}) if row else {}
            if performance.get('action') == 'show' and performance.get('expires', 0) > self.store.clock().timestamp():
                prompt += (f"\n【外观表演请求】用户要求展示{performance.get('label', '')}表情，展示请求已生成。"
                           "这不等于真实情绪改变；可以自然回应，但没有前端回执不能声称动作已成功完成。")
            elif pending:
                prompt += "\n若用户要求做表情，外观请求仍待评估；可以回应正在处理，不能保证已经执行。"
        if not prompt:
            return {"action": "continue"}
        modified = dict(kwargs)
        # 保留其它插件给出的 extra_prompt 和所有工具/模型参数；不回写永久人设。
        modified["extra_prompt"] = str(kwargs.get("extra_prompt") or "") + "\n\n" + prompt
        await asyncio.to_thread(self.store.append, "心情影响回复风格", session,
            message_id=str(kwargs.get("reply_message_id") or ""), mood=state, band=band, style_name=band if self.config.states.enabled else "",
            injected_prompt=prompt, assessment_pending=pending, emotion_wait_ms=wait_ms,
            reply_first=self.config.performance.reply_first, attempt=kwargs.get("attempt", 1), note="临时提示词已提交给回复构造流程；模型是否遵循需观察实际回复")
        return {"action": "continue", "modified_kwargs": modified}


def create_plugin():
    return MoodPlugin()
