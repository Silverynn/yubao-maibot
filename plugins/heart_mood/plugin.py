"""读取持久化心情，追加临时回复要求；不覆盖原有人设和历史。"""

import asyncio

from heart_shared.storage import AuditStore
from maibot_sdk import Field, HookHandler, MaiBotPlugin, PluginConfigBase
from maibot_sdk.types import HookMode, HookOrder
from pydantic import model_validator

from .appraiser import AIAppraiser
from .engine import MoodEngine


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
    enabled: bool = Field(default=False, description="开启后AI判断优先，不与关键词重复加分；会额外调用现有utils模型")
    fallback_keywords: bool = Field(default=False, description="仅AI失败时改用关键词；关闭则失败不计刺激，仍记录原因")
    timeout_seconds: float = Field(default=20, ge=1, le=60, description="单次AI等待上限秒数；后台评估不阻塞聊天，旧配置的6秒会保留")
    max_output_tokens: int = Field(default=4096, ge=256, le=16384, description="模型输出长度上限（不是心情分数）；过小可能截断，过大会增加费用与等待")
    delta_multiplier: float = Field(default=1, ge=0, le=3, description="AI变化倍率：0不计刺激，0.5更温和，1原建议，2更敏感；之后仍执行各项上限和冷却")
    positive_limit: float = Field(default=8, ge=0, le=20, description="AI每条最多加多少分；最终也不能超过mood.max_delta")
    negative_limit: float = Field(default=8, ge=0, le=20, description="AI每条最多减多少分（填正数）；最终也不能超过mood.max_delta")
    confidence_threshold: float = Field(default=0.7, ge=0, le=1, description="低于此把握程度保持不变，不转用关键词")
    guidance: str = Field(default="评价角色受到的互动影响而不是给用户打分；克制波动，尊重用户，普通知识提问通常保持不变。", max_length=2000, description="可修改的AI判断补充要求；不是回复风格提示词")


class Config(PluginConfigBase):
    plugin: Switch = Field(default_factory=Switch)
    mood: Settings = Field(default_factory=Settings)
    rules: Rules = Field(default_factory=Rules)
    prompts: Prompts = Field(default_factory=Prompts)
    states: States = Field(default_factory=States)
    ai: AISettings = Field(default_factory=AISettings)


class MoodPlugin(MaiBotPlugin):
    config_model = Config

    async def on_load(self):
        self.store = AuditStore()
        self.engine = MoodEngine(self.store)
        self.appraiser = AIAppraiser(self)
        self._get_logger().info("Heart心情插件已加载；数值保存在：%s", self.store.root)

    async def on_unload(self):
        if hasattr(self, "appraiser"):
            await self.appraiser.close()

    async def on_config_update(self, scope, config_data, version):
        """Runner 注入配置后下次请求生效；不重置已有心情数值。"""

    @HookHandler("chat.receive.after_process", mode=HookMode.BLOCKING, order=HookOrder.EARLY)
    async def incoming(self, message=None, **kwargs):
        if self.config.plugin.enabled and isinstance(message, dict):
            text = str(message.get("processed_plain_text") or "").strip()
            if self.config.ai.enabled and text and not text.startswith(("/", "!")) and not message.get("is_notify") and not message.get("is_command"):
                if not hasattr(self, "appraiser"):
                    self.appraiser = AIAppraiser(self)
                await self.appraiser.submit(message)
            else:
                await asyncio.to_thread(self.engine.update, message, self.config.mood, self.config.rules)
        return {"action": "continue"}

    @HookHandler("maisaka.replyer.before_request", mode=HookMode.BLOCKING, order=HookOrder.LATE)
    async def before_reply(self, **kwargs):
        if not self.config.plugin.enabled:
            return {"action": "continue"}
        session = str(kwargs.get("session_id") or "")
        prompt, state, band = await asyncio.to_thread(self.engine.style, session, self.config.prompts, self.config.states)
        if not prompt:
            return {"action": "continue"}
        modified = dict(kwargs)
        # 保留其它插件给出的 extra_prompt 和所有工具/模型参数；不回写永久人设。
        modified["extra_prompt"] = str(kwargs.get("extra_prompt") or "") + "\n\n" + prompt
        await asyncio.to_thread(self.store.append, "心情影响回复风格", session,
            message_id=str(kwargs.get("reply_message_id") or ""), mood=state, band=band, style_name=band if self.config.states.enabled else "",
            injected_prompt=prompt, attempt=kwargs.get("attempt", 1), note="临时提示词已提交给回复构造流程；模型是否遵循需观察实际回复")
        return {"action": "continue", "modified_kwargs": modified}


def create_plugin():
    return MoodPlugin()
