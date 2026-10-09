"""同机SenseVoice识别；与QQ共享CPU模型，不保存原始录音。"""
import asyncio
import io
import time
from urllib.parse import urlsplit
import wave

import aiohttp
import numpy as np

from .asr_interface import ASRInterface


class VoiceRecognition(ASRInterface):
    def __init__(self, endpoint_url='http://127.0.0.1:12395/v1/audio/transcriptions', timeout_seconds=25):
        url = urlsplit(endpoint_url)
        if url.scheme != 'http' or url.hostname not in {'127.0.0.1', 'localhost', '::1'}:
            raise ValueError('SenseVoice仅允许已安装的本机识别服务')
        self.endpoint = endpoint_url
        self.timeout = float(timeout_seconds)

    async def async_transcribe_np(self, audio):
        from dialogue_trace import current_turn
        trace = current_turn.get()
        started = time.monotonic()
        seconds = len(audio) / self.SAMPLE_RATE
        if trace:
            trace.stage('asr_started', _kind='语音识别', status='识别中',
                        source='Live2D麦克风', model='SenseVoiceSmall-int8/CPU', audio_seconds=seconds)
        try:
            if audio.ndim != 1 or not .15 <= seconds <= 90 or not np.isfinite(audio).all():
                raise ValueError('录音为空、过长或格式无效，请重新录音')
            buffer = io.BytesIO()
            with wave.open(buffer, 'wb') as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(self.SAMPLE_RATE)
                wav.writeframes((np.clip(audio, -1, 1) * 32767).astype('<i2').tobytes())
            body = aiohttp.FormData()
            body.add_field('file', buffer.getvalue(), filename='microphone.wav', content_type='audio/wav')
            body.add_field('language', 'auto')
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self.timeout), trust_env=False) as client:
                async with client.post(self.endpoint, data=body) as response:
                    if response.status != 200:
                        raise RuntimeError(f'本地识别服务返回HTTP {response.status}')
                    result = await response.json()
            text = str(result.get('text') or '').strip()
            if not text:
                raise ValueError('没有识别到有效讲话，请重新录音或发送文字')
            if trace:
                trace.stage('asr_completed', _kind='语音识别', status='识别成功',
                            source='Live2D麦克风', model=result.get('model', 'SenseVoiceSmall-int8'),
                            text=text, audio_seconds=seconds, duration_seconds=time.monotonic() - started,
                            sample_rate=self.SAMPLE_RATE)
            return text
        except asyncio.CancelledError:
            if trace:
                trace.stage('asr_cancelled', _kind='语音识别', status='识别已取消',
                            source='Live2D麦克风', model='SenseVoiceSmall-int8/CPU',
                            reason='用户打断或网页连接结束', duration_seconds=time.monotonic() - started)
            raise
        except Exception as error:
            if trace:
                trace.stage('asr_failed', _kind='语音识别', status='识别失败',
                            source='Live2D麦克风', model='SenseVoiceSmall-int8/CPU',
                            reason=str(error)[:160], duration_seconds=time.monotonic() - started,
                            audio_seconds=seconds)
            raise

    def transcribe_np(self, audio):
        return asyncio.run(self.async_transcribe_np(audio))
