"""每条 Live2D 连接拥有自己的语音上下文，不读取其他会话。"""
from contextvars import ContextVar

client_uid = ContextVar("moonbell_voice_client_uid", default=None)


def requested_voice(metadata):
    """只接受真正的布尔值；旧页面未传开关时保持语音开启。"""
    value = (metadata or {}).get('heart_voice_enabled', True)
    return value if type(value) is bool else True
