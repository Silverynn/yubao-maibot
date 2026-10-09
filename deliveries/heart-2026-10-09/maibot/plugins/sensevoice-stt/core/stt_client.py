"""SenseVoice HTTP 客户端 — 对接本地 Docker sidecar。"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from urllib.parse import urlsplit, urlunsplit

import aiohttp


@dataclass
class TranscriptionResult:
    """转录结果。

    Attributes:
        text: 转录文本（失败时为空字符串）。
        success: 是否成功。
        error_type: 失败原因分类
            (``"timeout"`` / ``"connection"`` / ``"http_error"`` / ``None``)。
        attempt: 实际尝试次数。
    """

    text: str
    success: bool
    error_type: str | None = None
    attempt: int = 1
    metadata: dict = field(default_factory=dict)

    @classmethod
    def ok(cls, text: str, attempt: int = 1) -> TranscriptionResult:
        return cls(text=text.strip(), success=True, attempt=attempt)

    @classmethod
    def fail(cls, error_type: str, attempt: int = 1) -> TranscriptionResult:
        return cls(text="", success=False, error_type=error_type, attempt=attempt)


@dataclass
class HealthCheckResult:
    """SenseVoice sidecar 健康检查结果。"""

    ok: bool
    status: str
    error_type: str | None = None

    @classmethod
    def ready(cls) -> HealthCheckResult:
        return cls(ok=True, status="ready")

    @classmethod
    def fail(cls, error_type: str, status: str = "unreachable") -> HealthCheckResult:
        return cls(ok=False, status=status, error_type=error_type)


def build_health_url(endpoint_url: str) -> str:
    """从转录 endpoint 推导 sidecar health endpoint。"""
    endpoint = endpoint_url.rstrip("/")
    parsed = urlsplit(endpoint)
    if not parsed.scheme or not parsed.netloc:
        return f"{endpoint}/health"

    path = parsed.path.rstrip("/")
    suffix = "/v1/audio/transcriptions"
    if path.endswith(suffix):
        path = path[: -len(suffix)]
    health_path = f"{path}/health" if path else "/health"
    return urlunsplit((parsed.scheme, parsed.netloc, health_path, "", ""))


class SenseVoiceClient:
    """本地 SenseVoice sidecar 的异步 HTTP 客户端。

    向 ``POST {endpoint_url}`` 发送 multipart/form-data 请求，
    字段 ``file``（WAV 音频）和 ``language``（语言标签）。

    本机分支只发一次限时请求，避免重试超过阻塞 Hook 的总时限。
    """

    _MAX_RETRIES = 1  # One bounded request fits the blocking hook budget.
    _RETRY_DELAY = 1.0

    def __init__(self, endpoint_url: str, timeout: float, language: str) -> None:
        parsed = urlsplit(endpoint_url)
        if parsed.scheme != 'http' or parsed.hostname not in {'127.0.0.1', 'localhost', '::1'}:
            raise ValueError('Only the installed local recognizer is allowed')
        self._endpoint = endpoint_url.rstrip("/")
        self._timeout_seconds = float(timeout)
        self._timeout = aiohttp.ClientTimeout(total=self._timeout_seconds)
        self._health_timeout = aiohttp.ClientTimeout(total=min(self._timeout_seconds, 3.0))
        self._language = language
        self._health_url = build_health_url(self._endpoint)

    @property
    def endpoint_url(self) -> str:
        return self._endpoint

    @property
    def health_url(self) -> str:
        return self._health_url

    async def transcribe(self, audio_path: str) -> TranscriptionResult:
        """发送音频到 SenseVoice sidecar，返回转录结果。

        Args:
            audio_path: 本地 WAV 文件的绝对路径。

        Returns:
            TranscriptionResult：成功时 ``text`` 包含转录文本，
            失败时 ``text`` 为空且 ``error_type`` 指示原因。
        """
        filename = os.path.basename(audio_path)

        for attempt in range(1, self._MAX_RETRIES + 1):
            try:
                result = await self._post_transcribe(audio_path, filename)
                if result.success:
                    result.attempt = attempt
                    return result
                if not result.success:
                    return result  # 非 200 不重试
            except asyncio.TimeoutError:
                if attempt < self._MAX_RETRIES:
                    await asyncio.sleep(self._RETRY_DELAY)
                    continue
                return TranscriptionResult.fail("timeout", attempt)
            except aiohttp.ClientConnectorError:
                if attempt < self._MAX_RETRIES:
                    await asyncio.sleep(self._RETRY_DELAY)
                    continue
                return TranscriptionResult.fail("connection", attempt)
            except Exception:
                return TranscriptionResult.fail("unknown", attempt)

            if attempt < self._MAX_RETRIES:
                await asyncio.sleep(self._RETRY_DELAY)

        return TranscriptionResult.fail("timeout", self._MAX_RETRIES)

    async def _post_transcribe(
        self, audio_path: str, filename: str
    ) -> TranscriptionResult:
        """单次 POST 请求，不重试。"""
        try:
            async with aiohttp.ClientSession(timeout=self._timeout, trust_env=False) as session:
                with open(audio_path, "rb") as audio_file:
                    data = aiohttp.FormData()
                    data.add_field(
                        "file",
                        audio_file,
                        filename=filename,
                        content_type="audio/wav",
                    )
                    data.add_field("language", self._language)
                    async with session.post(self._endpoint, data=data) as resp:
                        if resp.status == 200:
                            result = await resp.json()
                            text = (result.get("text") or "").strip()
                            outcome = TranscriptionResult.ok(text) if text else TranscriptionResult.fail('empty')
                            outcome.metadata = {key: result[key] for key in ('model', 'audio_seconds', 'duration_seconds', 'sample_rate', 'reason') if key in result}
                            return outcome
                        return TranscriptionResult.fail("http_error")
        except Exception:
            raise

    async def check_health(self) -> HealthCheckResult:
        """检查 SenseVoice sidecar 是否 ready。"""
        try:
            async with aiohttp.ClientSession(timeout=self._health_timeout, trust_env=False) as session:
                async with session.get(self._health_url) as resp:
                    if resp.status != 200:
                        return HealthCheckResult.fail("http_error", f"http_{resp.status}")
                    payload = await resp.json()
                    status = str(payload.get("status", "unknown"))
                    if status == "ready":
                        return HealthCheckResult.ready()
                    return HealthCheckResult.fail("not_ready", status)
        except asyncio.TimeoutError:
            return HealthCheckResult.fail("timeout")
        except aiohttp.ClientConnectorError:
            return HealthCheckResult.fail("connection")
        except Exception:
            return HealthCheckResult.fail("unknown")
