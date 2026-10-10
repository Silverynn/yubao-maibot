"""实际 SDK 配置、生命周期和模型参数测试；上下文模型接口全部模拟。"""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import asyncio
import importlib.util
import sys
import unittest
import tempfile


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("isolated_storyroom_sdk", ROOT / "plugins/storyroom_pc/plugin.py",
                                             submodule_search_locations=[str(ROOT / "plugins/storyroom_pc")])
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)


class SDKTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="storyroom-sdk-")
        self.addCleanup(self.temp.cleanup)

    def plugin(self, enabled=False):
        instance = module.create_plugin()
        instance.set_plugin_config({"plugin": {"config_version": "0.1.0"},
                                    "connection": {"enabled": enabled, "player_token": "isolated-player-token"}})
        ctx = SimpleNamespace(paths=SimpleNamespace(data_dir=Path(self.temp.name)),
                              call_capability=AsyncMock(return_value={"success": True, "response": "{}"}),
                              logger=SimpleNamespace(warning=lambda message: None))
        instance._set_context(ctx)
        return instance, ctx

    async def test_disabled_plugin_does_not_connect_or_call_model(self):
        instance, ctx = self.plugin()
        with patch.object(module, "RoomClient") as client:
            await instance.on_load()
            client.assert_not_called()
            ctx.call_capability.assert_not_called()
            self.assertIsNone(instance.task)
            await instance.on_unload()

    async def test_config_schema_contains_chinese_controls(self):
        instance, _ = self.plugin()
        schema = str(instance.build_config_schema())
        for text in ("启用跑团连接", "玩家连接密钥", "跑团角色设定", "模型等待上限"):
            self.assertIn(text, schema)

    async def test_enable_requires_key(self):
        instance, _ = self.plugin()
        instance.set_plugin_config({"plugin": {"config_version": "0.1.0"}, "connection": {"enabled": True}})
        with self.assertRaises(ValueError):
            await instance.on_load()

    async def test_sdk_timeout_and_routing_arguments(self):
        instance, ctx = self.plugin(True)
        with patch.object(module, "PCWorker") as worker:
            worker.return_value.run = AsyncMock()
            await instance.on_load()
            generate = worker.call_args.args[1]
            await generate("仅用于测试的公开剧情")
            arguments = ctx.call_capability.await_args.kwargs
            self.assertEqual(arguments["task_name"], "replyer")
            self.assertEqual(arguments["request_timeout_seconds"], 40)
            self.assertEqual(arguments["max_tokens"], 4096)
            self.assertEqual(arguments["timeout_ms"], 42000)
            await instance.on_unload()
            self.assertIsNone(instance.task)

    async def test_unload_cancels_worker(self):
        instance, _ = self.plugin(True)
        with patch.object(module, "PCWorker") as worker:
            # 使用真正的异步暂停函数，验证取消能传递到后台任务。
            async def wait_forever():
                await asyncio.Event().wait()
            worker.return_value.run = wait_forever
            await instance.on_load()
            await asyncio.sleep(0)
            await instance.on_unload()
            self.assertIsNone(instance.task)

    async def test_hot_disable_stops_worker(self):
        instance, _ = self.plugin(True)
        with patch.object(module, "PCWorker") as worker:
            async def wait_forever():
                await asyncio.Event().wait()
            worker.return_value.run = wait_forever
            await instance.on_load()
            instance.set_plugin_config({"plugin": {"config_version": "0.1.0"}, "connection": {"enabled": False}})
            await instance.on_config_update("self", {}, 2)
            self.assertIsNone(instance.task)
