"""调用独立的本机 Qwen3-TTS 声带；Live2D 环境不导入 GPU 模型。"""
from pathlib import Path
import asyncio
import base64
import json
import uuid

import httpx
from loguru import logger

from .tts_interface import TTSInterface


class TTSEngine(TTSInterface):
    supports_early_text = True
    prefer_short_first_chunk = True
    def __init__(self, service_url="http://127.0.0.1:12394", token_file="", timeout=65):
        # 不允许配置成远端地址，避免把对话意外发给外部语音服务。
        if service_url.rstrip("/") not in {"http://127.0.0.1:12394", "http://localhost:12394"}:
            raise ValueError("本地语音地址必须是本机12394端口")
        self.service_url = service_url.rstrip("/")
        self.token_file = Path(token_file)
        self.timeout = timeout
        self._lock = asyncio.Lock()

    def generate_audio(self, text, file_name_no_ext=None):
        raise RuntimeError("本地语音使用异步接口，请调用 async_generate_audio")

    async def async_generate_audio(self, text, file_name_no_ext=None):
        from voice_context import client_uid
        from heart_bridge import voice_snapshot
        async with self._lock:
            # 排队期间情绪可能更新；真正轮到合成时才取状态，不沿用队首旧快照。
            from dialogue_trace import current_turn
            trace = current_turn.get()
            context = voice_snapshot(client_uid.get(), message_id=trace.message_id if trace else None)
            if trace:
                context['turn_id'] = trace.turn_id
                context.setdefault('assessment_pending', True)
            return await self._generate(text, file_name_no_ext, context)

    @staticmethod
    def _record(context, request_id, text, status, style, elapsed):
        # 观测失败不能把已合成成功的声音变成“合成失败”；明确报告日志异常。
        from heart_bridge import record_voice
        try:
            record_voice(context, request_id, text, status, style, elapsed)
        except Exception as error:
            logger.warning("语音事件写入日志失败，语音/文字继续交付：{}", type(error).__name__)

    async def _generate(self, text, file_name_no_ext, context):
        request_id = uuid.uuid4().hex
        audio_path = None
        try:
            token = await asyncio.to_thread(self.token_file.read_text, encoding="utf-8")
            # 短段依次合成；既不会占满显存，也不启动额外的聊天模型。
            # MaiBot 当前已按句输出；长句进一步切小，不改变显示台词。
            pieces = [text[i:i + 100] for i in range(0, len(text), 100)]
            import io
            import soundfile as sf
            import numpy as np
            samples, sample_rate, elapsed, style = [], None, 0.0, "neutral"
            generation_runs = []
            observation = {}
            async with httpx.AsyncClient(timeout=self.timeout, trust_env=False, follow_redirects=False) as client:
                for index, piece in enumerate(pieces):
                    response = await client.post(self.service_url + "/synthesize",
                        headers={"X-Moonbell-Token": token.strip()},
                        json={"text": piece, "context": context, "request_id": request_id + f"_{index}"})
                    response.raise_for_status()
                    waveform, rate = sf.read(io.BytesIO(response.content), dtype="float32")
                    if sample_rate is not None and rate != sample_rate:
                        raise ValueError("语音采样率发生变化")
                    sample_rate = rate
                    samples.append(waveform)
                    elapsed += float(response.headers.get("X-Voice-Seconds", "0"))
                    style = response.headers.get("X-Voice-Style", "neutral")
                    metadata = response.headers.get('X-Voice-Metadata')
                    if metadata:
                        actual = json.loads(base64.b64decode(metadata).decode('utf-8'))
                        context['voice_instruction'] = actual.get('instruction', '')
                        context['voice_name'] = actual.get('speaker', '')
                        context['voice_reason'] = actual.get('reason', '')
                        context['voice_model'] = actual.get('model', '')
                        context['voice_language'] = actual.get('language', '')
                        if isinstance(actual.get('generation'), dict):
                            # 此项是音频编码长度上限，不是访问令牌；用准确的展示字段名，
                            # 避免日志通用脱敏规则把 max_new_tokens 误遮掉，绝不放宽密钥保护。
                            generation_runs.append({
                                'audio_code_limit' if key == 'max_new_tokens' else key: value
                                for key, value in actual['generation'].items()})
                        if actual.get('parameter_status'):
                            context['voice_parameter_status'] = actual['parameter_status']
                        observation = actual.get('observation') or {}
            waveform = np.concatenate(samples)
            context['voice_generation_runs'] = generation_runs
            if observation.get('enabled', True):
                try:
                    from speech_observation import observe_audio
                    context['voice_acoustics'] = await asyncio.to_thread(
                        observe_audio, waveform, sample_rate, text, observation)
                except Exception as error:
                    context['voice_acoustics'] = {'status':'音频观察失败：' + type(error).__name__}
                    logger.warning("音频观察未完成，声音继续交付：{}", type(error).__name__)
            else:
                context['voice_acoustics'] = {'status':'用户关闭音频观察'}
            audio_path = self.generate_cache_file_name(file_name_no_ext or request_id, "wav")
            await asyncio.to_thread(sf.write, audio_path, waveform, sample_rate, subtype="PCM_16")
            self._record(context, request_id, text, "合成成功（尚未播放）", style, elapsed)
            return audio_path
        except asyncio.CancelledError:
            self._record(context, request_id, text, "用户打断，停止等待语音", "", None)
            raise
        except Exception as error:
            logger.warning("本地语音生成未成功，继续保留文字：{}", type(error).__name__)
            self._record(context, request_id, text, "合成失败，保留文字：" + type(error).__name__, "", None)
            raise
