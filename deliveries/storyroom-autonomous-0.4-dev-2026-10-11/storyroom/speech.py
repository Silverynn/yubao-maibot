"""复用平台主机已有的本地ASR，不安装模型，不保存录音，不接触QQ语音Hook。"""
from io import BytesIO
from urllib.parse import urlsplit
import base64
import wave

import httpx


def validate_endpoint(url: str):
    parsed = urlsplit(url)
    if parsed.scheme != 'http' or parsed.hostname not in ('127.0.0.1', 'localhost', '::1') or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('ASR地址只能是平台主机的本地HTTP服务，不能带密钥或查询参数')
    return url


def decode_wav(encoded: str):
    raw = base64.b64decode(encoded, validate=True)
    if len(raw) > 960044:
        raise ValueError('录音超过30秒上限')
    with wave.open(BytesIO(raw), 'rb') as audio:
        if audio.getnchannels() != 1 or audio.getsampwidth() != 2 or audio.getframerate() != 16000 or audio.getcomptype() != 'NONE':
            raise ValueError('只接受16kHz、16bit、单声道PCM WAV')
        seconds = audio.getnframes() / 16000
        if not .1 <= seconds <= 30 or len(audio.readframes(audio.getnframes())) != audio.getnframes() * 2:
            raise ValueError('录音长度或内容不合法')
    return raw, seconds


async def transcribe(url: str, raw: bytes):
    async with httpx.AsyncClient(timeout=20, trust_env=False, follow_redirects=False) as client:
        response = await client.post(validate_endpoint(url), files={'file': ('storyroom.wav', raw, 'audio/wav')}, data={'language': 'auto'})
        response.raise_for_status()
        text = response.json().get('text', '')
        if not isinstance(text, str) or not text.strip() or len(text) > 2000:
            raise ValueError('识别没有有效文字或结果过长')
        return text.strip()
