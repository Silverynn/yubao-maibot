"""MaiBot SDK 插件，只借用模型服务，不干预日常聊天、记忆或心情 Hook。"""
from contextlib import suppress
from typing import Optional
import asyncio

from maibot_sdk import Field, MaiBotPlugin, PluginConfigBase

from .worker import Audit, PCWorker, RoomClient


class Metadata(PluginConfigBase):
    __ui_label__ = "插件信息"
    config_version: str = Field(default="0.3.0", description="配置版本，无需手动修改", json_schema_extra={"label": "配置版本"})


class Connection(PluginConfigBase):
    __ui_label__ = "跑团房间连接"
    enabled: bool = Field(default=False, description="开启后持续旁听；主持人开放讨论或点名时才调用模型，提问和行动等待主持人回应", json_schema_extra={"label": "启用跑团连接"})
    server_url: str = Field(default="http://127.0.0.1:8790", description="异地须用HTTPS，只填平台地址", json_schema_extra={"label": "共享房间地址"})
    player_token: str = Field(default="", description="主持人分配给这个PC的专用密钥，不是MaiBot管理员token", json_schema_extra={"label": "玩家连接密钥", "x-widget": "password"})
    trust_env: bool = Field(default=False, description="默认直连；仅需要系统代理时开启，仍检查HTTPS证书且禁止重定向", json_schema_extra={"label": "使用系统代理"})
    poll_seconds: float = Field(default=2, ge=1, le=30, json_schema_extra={"label": "同步房间与检查发言间隔（秒）"})


class Character(PluginConfigBase):
    __ui_label__ = "玩家角色与模型"
    identity: str = Field(default="我是此房间指定的玩家角色，按照主持人确认的角色卡保持身份与行为连续。", max_length=4000, description="只填写愿意带入共享跑团房间的角色设定，不自动读取QQ记忆", json_schema_extra={"label": "跑团角色设定"})
    guidance: str = Field(default="用符合角色的自然口吻，发言控制在100字左右，行动提案明确简短。", max_length=2000, json_schema_extra={"label": "玩家输出风格"})
    model_name: str = Field(default="", max_length=128, description="留空使用MaiBot replyer任务模型；可指定配置中的模型name", json_schema_extra={"label": "玩家专用模型（可留空）"})
    timeout_seconds: float = Field(default=40, ge=5, le=60, description="调用超时会记录失败，不假装玩家已经行动", json_schema_extra={"label": "模型等待上限（秒）"})
    max_tokens: int = Field(default=4096, ge=1024, le=16384, description="部分推理模型还需要思考预算，不建议设置成400", json_schema_extra={"label": "模型输出预算"})


class Config(PluginConfigBase):
    plugin: Metadata = Field(default_factory=Metadata)
    connection: Connection = Field(default_factory=Connection)
    character: Character = Field(default_factory=Character)


class StoryRoomPC(MaiBotPlugin):
    config_model = Config

    async def on_load(self):
        self.task: Optional[asyncio.Task] = None
        self.client = None
        self.audit = Audit(self.ctx.paths.data_dir / "日志")
        await self.restart()

    async def on_unload(self):
        if self.task:
            if not self.task.done():
                self.task.cancel()
                with suppress(asyncio.CancelledError):
                    await self.task
            self.task = None
        if self.client:
            try:
                await asyncio.wait_for(self.client.request("/api/bot/state", {"enabled": False, "version": "0.3.0"}), 2)
            except Exception as error:
                self.audit.emit("停止回执未送达", enabled=False, error_type=type(error).__name__)
            self.client = None
        self.audit.emit("插件停止", enabled=False)

    async def on_config_update(self, scope, config_data, version):
        if scope == "self":
            await self.on_unload()
            await self.restart()

    async def restart(self):
        c = self.config
        self.audit.emit("插件配置", enabled=c.connection.enabled, model_name=c.character.model_name or "replyer",
                        max_tokens=c.character.max_tokens, timeout_seconds=c.character.timeout_seconds)
        self.ctx.logger.warning("Heart跑团PC连接：" + ("已开启" if c.connection.enabled else "已关闭") + "；不拦截QQ聊天")
        if not c.connection.enabled:
            return
        if len(c.connection.player_token) < 16:
            raise ValueError("开启跑团前请填写主持人分配的玩家连接密钥")
        client = RoomClient(c.connection.server_url, c.connection.player_token, c.connection.trust_env)
        self.client = client

        async def generate(prompt):
            result = await self.ctx.call_capability(
                "llm.generate", prompt=prompt, task_name="replyer",
                model_name=c.character.model_name, max_tokens=c.character.max_tokens,
                request_timeout_seconds=c.character.timeout_seconds,
                timeout_ms=int((c.character.timeout_seconds + 2) * 1000))
            if not isinstance(result, dict) or result.get("success") is False:
                raise RuntimeError("原生模型接口未成功返回")
            return result.get("response", "")

        worker = PCWorker(client, generate, c.character.identity, c.character.guidance,
                          c.character.timeout_seconds + 2, lambda text: self.ctx.logger.warning(text),
                          interval=c.connection.poll_seconds, data_dir=self.ctx.paths.data_dir, audit=self.audit)
        self.task = asyncio.create_task(worker.run(), name="storyroom-pc-poll")
        def finished(task):
            if not task.cancelled() and task.exception() is not None:
                error = task.exception()
                self.audit.emit("后台任务停止", enabled=True, error_type=type(error).__name__, detail=str(error))
                self.ctx.logger.warning("跑团插件后台已停止：" + str(error))
        self.task.add_done_callback(finished)


def create_plugin():
    return StoryRoomPC()
