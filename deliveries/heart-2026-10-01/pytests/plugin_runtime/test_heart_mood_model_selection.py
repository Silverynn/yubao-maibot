"""心情插件独立模型选择的兼容性回归测试，不调用真实模型。"""

from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, MagicMock, Mock

import asyncio
import time

from plugins.heart_mood.appraiser import AIAppraiser
from plugins.heart_mood.plugin import Config, MoodPlugin


class HeartMoodModelSelectionTests(IsolatedAsyncioTestCase):
    def test_old_config_keeps_utils_default_and_webui_shows_setting(self):
        config = Config.model_validate({"ai": {"enabled": True}})
        self.assertEqual(config.ai.model_name, "")

        field = MoodPlugin.build_config_schema()["sections"]["ai"]["fields"]["model_name"]
        self.assertEqual(field["default"], "")
        self.assertIn("专用模型", field["label"])

    async def test_selected_model_reaches_llm_capability(self):
        for configured_name, expected_name in (("", None), ("  dedicated-fast  ", "dedicated-fast")):
            with self.subTest(model_name=configured_name):
                config = Config.model_validate({"ai": {"enabled": True, "model_name": configured_name}})
                store = SimpleNamespace(
                    connect=MagicMock(return_value=MagicMock()),
                    mood_snapshot=Mock(return_value={"value": 50}),
                    emotion_snapshot=Mock(return_value={}),
                )
                capability = AsyncMock(return_value={
                    "success": True,
                    "response": '{"delta":0,"confidence":0.9,"reason":"普通提问","evidence":""}',
                    "completion_tokens": 30,
                })
                plugin = SimpleNamespace(
                    config=config,
                    store=store,
                    ctx=SimpleNamespace(call_capability=capability),
                )
                appraiser = AIAppraiser(plugin)
                appraiser.apply = AsyncMock()
                key = ("test-session", "test-message")
                snapshot = {"当前原话": "你好", "心情快照": 50, "当前话题情绪": {}}
                appraiser.queue.put_nowait((key, object(), snapshot, config, time.monotonic(), [key], []))
                worker = asyncio.create_task(appraiser.run())
                try:
                    await asyncio.wait_for(appraiser.queue.join(), timeout=2)
                finally:
                    worker.cancel()
                    await asyncio.gather(worker, return_exceptions=True)

                capability.assert_awaited_once()
                args, kwargs = capability.await_args
                self.assertEqual(args, ("llm.generate",))
                self.assertEqual(kwargs["task_name"], "utils")
                self.assertEqual(kwargs["model_name"], expected_name)
                appraiser.apply.assert_awaited_once()
