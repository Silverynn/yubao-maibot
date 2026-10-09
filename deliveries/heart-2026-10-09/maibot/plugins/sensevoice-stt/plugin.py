"""SenseVoice STT 插件 — 将 QQ 语音消息转录为文字。

通过 ``@HookHandler("chat.receive.before_process")`` 在消息到达 LLM 之前
拦截语音段，调用本地或远程 SenseVoice sidecar 转录，原地替换为文本段，
并重建 ``processed_plain_text``。

纯本地方案：不依赖任何第三方云 API。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from maibot_sdk import HomeCard, HookHandler, MaiBotPlugin
from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder

from heart_shared.storage import AuditStore
from heart_shared.card_writer import start_writer

from .core.audio_utils import cleanup_temp_file, decode_base64_to_wav
from .core.config import SenseVoiceSttPluginConfig
from .core.stt_client import HealthCheckResult, SenseVoiceClient, TranscriptionResult


class SenseVoiceSttPlugin(MaiBotPlugin):
    """SenseVoice STT 插件主类。"""

    config_model = SenseVoiceSttPluginConfig

    _MAX_CONSECUTIVE_FAIL_LOG = 3  # 连续失败达此阈值时记 WARNING

    def __init__(self) -> None:
        super().__init__()
        self._audit_store = None
        self._client: SenseVoiceClient | None = None
        self._stats: dict[str, int] = {}
        self._started_at: float = 0.0
        self._last_error_type: str | None = None
        self._reset_stats()

    def _reset_stats(self) -> None:
        self._stats = {
            "total_requests": 0,
            "total_success": 0,
            "total_failure": 0,
            "consecutive_failures": 0,
        }
        self._started_at = time.time()
        self._last_error_type = None

    # ===== 生命周期（SDK 强制）=====

    async def on_load(self) -> None:
        self._audit_store = AuditStore()
        start_writer(self._audit_store)  # Existing Heart plugins own retention; no extra startup pruning.
        self._init_client()
        self._reset_stats()
        self.ctx.logger.info(
            "SenseVoice STT 插件已加载，后端: %s",
            self.config.stt.endpoint_url,
        )

    async def on_unload(self) -> None:
        self._client = None
        self.ctx.logger.info("SenseVoice STT 插件已卸载")

    async def on_config_update(
        self, scope: str, config_data: dict, version: str
    ) -> None:
        if scope == "self":
            self._init_client()
            self.ctx.logger.info("SenseVoice STT 配置已更新 (version=%s)", version)

    # ===== 内部 =====

    def _init_client(self) -> None:
        if self.config.plugin.enabled:
            self._client = SenseVoiceClient(
                endpoint_url=self.config.stt.endpoint_url,
                timeout=self.config.stt.timeout_seconds,
                language=self.config.stt.language,
            )
        else:
            self._client = None

    def _record_transcription(self, result: TranscriptionResult) -> None:
        self._stats["total_requests"] += 1
        if result.success:
            self._stats["total_success"] += 1
            self._stats["consecutive_failures"] = 0
            self._last_error_type = None
        else:
            self._stats["total_failure"] += 1
            self._stats["consecutive_failures"] += 1
            self._last_error_type = result.error_type or "unknown"
            if self._stats["consecutive_failures"] == self._MAX_CONSECUTIVE_FAIL_LOG:
                self.ctx.logger.warning(
                    "SenseVoice STT 连续失败 %d 次 (type=%s)",
                    self._stats["consecutive_failures"],
                    result.error_type,
                )

    def _uptime_hours(self) -> float:
        return (time.time() - self._started_at) / 3600.0

    async def _check_sidecar_health(self) -> HealthCheckResult:
        if not self._client:
            return HealthCheckResult.fail("disabled", "disabled")
        return await self._client.check_health()

    # ===== HookHandler =====

    @HookHandler(
        "chat.receive.before_process",
        name="sensevoice_stt_transcriber",
        description="将语音消息段转录为文字",
        mode=HookMode.BLOCKING,
        order=HookOrder.EARLY,
        timeout_ms=30000,
        error_policy=ErrorPolicy.ABORT,
    )
    async def transcribe_voice_segments(
        self, message: dict[str, Any] | None = None, **kwargs: Any
    ) -> dict[str, Any] | None:
        """拦截入站消息，将 voice 段转录为 text 段。

        Hook 参数由 ``chat.receive.before_process`` 注入，
        ``message`` 包含 ``raw_message``（段列表）和
        ``processed_plain_text``（纯文本表示）。
        """
        if not self._client or not message:
            return None
        info = message.get('message_info') or {}
        user = info.get('user_info') or {}
        platform = str(info.get('platform') or user.get('platform') or message.get('platform') or '').lower()
        if platform != 'qq':
            return None
        raw = message.get('raw_message')
        if not isinstance(raw, list):
            return None
        voices = [(i, seg) for i, seg in enumerate(raw) if isinstance(seg, dict) and
                  (seg.get('type') == 'voice' or (seg.get('type') == 'text' and seg.get('data') == '[voice]'))]
        if not voices:
            return None
        store = self._audit_store or AuditStore()
        fields = store.message_fields(message)
        session, message_id = fields['session'], fields['message_id']
        if not session or not message_id:
            # Native host assigns the routing/session ID before this hook.
            self.ctx.logger.error('语音识别缺少原生会话/消息编号，已停止处理')
            return {'action':'abort'}
        # Register provenance, but never save a placeholder as the user's dialogue.
        with store.transaction() as db:
            db.execute("INSERT INTO sessions VALUES(?,?,?) ON CONFLICT(session) DO UPDATE SET chat_type=excluded.chat_type,title=excluded.title",
                       (session, fields['chat_type'], fields['title']))
            db.execute("INSERT INTO session_sources VALUES(?,?,?,?) ON CONFLICT(session) DO UPDATE SET platform=excluded.platform,user_id=excluded.user_id,channel=excluded.channel",
                       (session, 'qq', fields['user'], 'QQ'))

        def audit(index: int | str, phase: str, **data: Any) -> None:
            store.append('语音识别', session, event_id=f'asr:{session}:{message_id}:{index}:{phase}',
                         message_id=message_id, source='QQ语音消息', reply_mode='text',
                         model='SenseVoiceSmall-int8/CPU', **data)

        async def one(index: int, segment: dict[str, Any]) -> tuple[int, dict[str, Any], TranscriptionResult]:
            started = time.monotonic()
            temporary = None
            audit(index, 'started', status='识别中')
            try:
                b64 = segment.get('binary_data_base64') or ''
                if len(voices) > 4:
                    result = TranscriptionResult.fail('too_many_segments')
                elif not b64:
                    result = TranscriptionResult.fail('audio_unavailable')
                else:
                    temporary = decode_base64_to_wav(b64, self.ctx.paths.runtime_dir)
                    result = await self._client.transcribe(temporary)
                self._record_transcription(result)
                metadata = dict(result.metadata)
                metadata.pop('model', None)
                metadata.pop('duration_seconds', None)
                reason = metadata.pop('reason', None) or result.error_type or ''
                audit(index, 'completed', status='识别成功' if result.success else '识别失败',
                      text=result.text if result.success else '', reason=reason,
                      duration_seconds=time.monotonic()-started, **metadata)
                return index, segment, result
            except asyncio.CancelledError:
                audit(index, 'cancelled', status='识别已取消', reason='消息处理取消或Hook超时',
                      duration_seconds=time.monotonic()-started)
                raise
            except Exception as error:
                result = TranscriptionResult.fail('invalid_audio' if isinstance(error, ValueError) else type(error).__name__)
                self._record_transcription(result)
                audit(index, 'failed', status='识别失败', reason=result.error_type,
                      duration_seconds=time.monotonic()-started)
                return index, segment, result
            finally:
                if temporary:
                    cleanup_temp_file(temporary)

        outcomes = await asyncio.gather(*(one(index, seg) for index, seg in voices))
        if any(not result.success for _, _, result in outcomes):
            # A failure message is system feedback, never an invented user utterance.
            sent = False
            try:
                sent = bool(await asyncio.wait_for(self.ctx.send.text(
                    '这条语音没能完整识别，请重新录音或发送文字。', session), timeout=3))
            except Exception:
                self.ctx.logger.warning('语音识别失败提示未确认送达')
            audit('notification', 'result', status='已停止AI处理',
                  reason='存在未识别语音；失败提示已发送' if sent else '存在未识别语音；失败提示未确认送达')
            return {'action':'abort'}
        for _, segment, result in outcomes:
            segment.clear()
            segment.update(type='text', data=result.text)
        message['processed_plain_text'] = rebuild_plain_text(raw)
        message['display_message'] = message['processed_plain_text']
        self.ctx.logger.info('语音转写已交给原生对话链路：%d段；QQ仅文字回复', len(outcomes))
        return {'action':'continue', 'modified_kwargs':{'message':message}}

    # ===== HomeCard =====

    @HomeCard(
        name="sensevoice_stt_status",
        title="语音转文字状态",
        description="SenseVoice STT 插件运行状态与统计",
        content="查看插件启用状态、后端健康和转录统计。",
        icon="\U0001F399",
        link_url="/plugin-config?plugin=dmjxmj.sensevoice-stt",
        link_label="插件配置",
        width="medium",
        order=100,
    )
    async def stt_home_card(self) -> dict[str, Any]:
        s = self._stats
        enabled = "启用" if self._client else "已禁用"
        endpoint = self.config.stt.endpoint_url
        if self._client:
            health = await self._check_sidecar_health()
            sidecar_status = (
                health.status if health.ok else f"{health.status}/{health.error_type}"
            )
        else:
            sidecar_status = "未启用"
        uptime_h = self._uptime_hours()
        rate_str = ""
        if s["total_requests"] > 0:
            rate = s["total_success"] / s["total_requests"] * 100
            rate_str = f"{rate:.1f}%"
        else:
            rate_str = "N/A"

        return {
            "blocks": [
                {
                    "type": "key_value",
                    "entries": {
                        "插件状态": enabled,
                        "后端健康": sidecar_status,
                        "Endpoint": endpoint,
                        "运行时间": f"{uptime_h:.1f}h",
                        "总请求": str(s["total_requests"]),
                        "成功率": rate_str,
                        "连续失败": str(s["consecutive_failures"]),
                        "最近错误": self._last_error_type or "无",
                    },
                },
                {
                    "type": "list",
                    "items": [
                        "本机共享识别服务：http://127.0.0.1:12395；QQ仅文字回复",
                        "成功/失败/取消均记录到Heart消息卡片；录音临时文件识别后删除",
                    ],
                }
            ]
        }


def rebuild_plain_text(segments: list[dict[str, Any]]) -> str:
    """从段列表重建纯文本表示。

    与 ``adapter/codecs/inbound/text.py:build_plain_text`` 逻辑对齐，
    确保 LLM 收到的文本格式一致。
    """
    parts: list[str] = []
    for seg in segments:
        if not isinstance(seg, dict):
            continue
        t = seg.get("type", "")
        if t == "text":
            parts.append(str(seg.get("data", "")))
        elif t == "voice":
            parts.append("[voice]")
        elif t == "image":
            parts.append("[image]")
        elif t == "reply":
            parts.append("[reply]")
    text = "".join(parts).strip()
    return text or "[unsupported]"


def create_plugin() -> SenseVoiceSttPlugin:
    """MaiBot 插件入口工厂函数（SDK 强制要求）。"""
    return SenseVoiceSttPlugin()
