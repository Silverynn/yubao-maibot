"""SenseVoice STT 插件 — Pydantic 配置模型。"""

from maibot_sdk import Field, PluginConfigBase


class PluginSection(PluginConfigBase):
    """插件基础配置，对应 config.toml 的 [plugin] 节。"""

    __ui_label__ = "插件设置"
    __ui_icon__ = "settings"
    __ui_order__ = 0

    enabled: bool = Field(
        default=True,
        description="是否启用语音转文字",
        json_schema_extra={"group": "plugin"},
    )
    config_version: str = Field(
        default="0.3.1",
        description="配置版本",
        json_schema_extra={"group": "plugin"},
    )


class SttConfig(PluginConfigBase):
    """STT 后端配置。"""

    __ui_label__ = "SenseVoice 后端"
    __ui_icon__ = "record_voice_over"
    __ui_order__ = 10

    endpoint_url: str = Field(
        default="http://127.0.0.1:12395/v1/audio/transcriptions",
        description=(
            "SenseVoice sidecar 转录接口。Docker/Compose 默认使用 "
            "http://127.0.0.1:12395/v1/audio/transcriptions；源码或一键包同机部署通常使用 "
            "http://127.0.0.1:12395/v1/audio/transcriptions"
        ),
        json_schema_extra={
            "placeholder": "http://127.0.0.1:12395/v1/audio/transcriptions",
            "group": "stt",
        },
    )
    timeout_seconds: int = Field(
        default=20,
        description="转录请求超时（秒）",
        json_schema_extra={"placeholder": "20", "group": "stt"},
    )
    language: str = Field(
        default="auto",
        description="识别语言 (auto/zh/en/yue/ja/ko)",
        json_schema_extra={"placeholder": "auto", "group": "stt"},
    )


class SenseVoiceSttPluginConfig(PluginConfigBase):
    """插件根配置。"""

    plugin: PluginSection = Field(default_factory=PluginSection)
    stt: SttConfig = Field(default_factory=SttConfig)
