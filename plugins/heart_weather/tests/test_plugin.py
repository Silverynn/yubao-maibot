"""天气插件自身的离线测试，不启动MaiBot、不发QQ、不访问真实天气/模型。"""

from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
import asyncio
import importlib.util
import sys
import unittest

import httpx
from maibot_sdk import MaiBotPlugin

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('weather_plugin_test', ROOT / 'plugin.py',
                                             submodule_search_locations=[str(ROOT)])
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
WeatherPlugin = module.WeatherPlugin


class PluginTests(unittest.IsolatedAsyncioTestCase):
    def make_plugin(self):
        plugin = WeatherPlugin()
        plugin.set_plugin_config(plugin.build_default_config())
        plugin._set_context(SimpleNamespace(send=SimpleNamespace(text=AsyncMock()), logger=Mock()))
        plugin._make_client()
        return plugin

    async def test_sdk_components_config_and_lifecycle(self):
        plugin = self.make_plugin()
        self.assertEqual({c['name'] for c in plugin.get_components()}, {'query_weather', 'weather'})
        for method in ('on_load', 'on_unload', 'on_config_update'):
            self.assertIsNot(getattr(type(plugin), method), getattr(MaiBotPlugin, method))
        await plugin.on_load()
        await plugin.on_config_update('self', plugin.build_default_config(), '1.0.1')
        await plugin.on_unload()
        schema = plugin.build_config_schema()
        self.assertEqual(schema['sections']['plugin']['fields']['timeout_seconds']['label'], '查询最长等待（秒）')

    async def test_missing_shared_audit_still_replies_once(self):
        plugin = self.make_plugin()
        plugin.client.fetch = AsyncMock(return_value={'success': True, 'content': '隔离测试：陕西西安天气'})
        with patch.dict(sys.modules, {'heart_shared': None, 'heart_shared.tool_audit': None}):
            result = await plugin.weather_command(stream_id='isolated-session',
                                                  matched_groups={'city': '西安 '},
                                                  message={'message_id': 'isolated-message'})
        self.assertEqual(result, (True, '隔离测试：陕西西安天气', True))
        plugin.client.fetch.assert_awaited_once_with('西安', 1)
        plugin.ctx.send.text.assert_awaited_once_with('隔离测试：陕西西安天气', 'isolated-session')
        self.assertIn('隔离测试：陕西西安天气', plugin.ctx.logger.info.call_args.args[0])
        plugin.ctx.logger.warning.assert_called_once()

    async def test_existing_shared_audit_receives_exact_source(self):
        plugin = self.make_plugin()
        plugin.client.fetch = AsyncMock(return_value={'success': True, 'content': '隔离天气'})
        audit = ModuleType('heart_shared.tool_audit')
        audit.record_tool = Mock()
        with patch.dict(sys.modules, {'heart_shared': ModuleType('heart_shared'), 'heart_shared.tool_audit': audit}):
            await plugin.weather_command(stream_id='isolated-session', matched_groups={'city': '西安'},
                                         message={'message_id': 'isolated-message'})
        self.assertEqual(audit.record_tool.call_args.kwargs['evidence_ids'], ['isolated-message'])
        self.assertEqual(audit.record_tool.call_args.args[0], 'isolated-session')

    async def test_shared_audit_failure_never_repeats_weather(self):
        plugin = self.make_plugin()
        plugin.client.fetch = AsyncMock(return_value={'success': True, 'content': '隔离天气'})
        audit = ModuleType('heart_shared.tool_audit')
        audit.record_tool = Mock(side_effect=OSError('isolated disk failure'))
        with patch.dict(sys.modules, {'heart_shared': ModuleType('heart_shared'), 'heart_shared.tool_audit': audit}):
            result = await plugin.weather_command(stream_id='isolated-session', matched_groups={'city': '西安'})
        self.assertTrue(result[0])
        plugin.client.fetch.assert_awaited_once()
        plugin.ctx.send.text.assert_awaited_once()
        plugin.ctx.logger.exception.assert_called_once()

    async def test_disabled_never_requests(self):
        plugin = self.make_plugin()
        config = plugin.build_default_config()
        config['plugin']['enabled'] = False
        plugin.set_plugin_config(config)
        plugin.client.fetch = AsyncMock()
        result = await plugin.query_weather(city='西安')
        self.assertFalse(result['success'])
        plugin.client.fetch.assert_not_awaited()

    async def test_timeout_is_failed_and_logged(self):
        plugin = self.make_plugin()
        plugin.client.fetch = AsyncMock(side_effect=TimeoutError())
        result = await plugin.query_weather(city='西安')
        self.assertFalse(result['success'])
        self.assertIn('超时', result['content'])
        self.assertIn('失败', plugin.ctx.logger.info.call_args.args[0])

    async def test_cancellation_propagates(self):
        plugin = self.make_plugin()
        plugin.client.fetch = AsyncMock(side_effect=asyncio.CancelledError())
        with self.assertRaises(asyncio.CancelledError):
            await plugin.query_weather(city='西安')
        plugin.ctx.logger.info.assert_called_once()

    async def test_mock_network_success_cache_and_no_private_parameters(self):
        requests = []
        def handle(request):
            requests.append(request)
            if 'geocoding' in request.url.host:
                return httpx.Response(200, json={'results': [{'name': '西安', 'country': '中国',
                    'admin1': '陕西', 'feature_code': 'PPLA', 'latitude': 34.26, 'longitude': 108.94}]})
            return httpx.Response(200, json={'timezone': 'Asia/Shanghai',
                'daily_units': {'temperature_2m_min': '°C', 'temperature_2m_max': '°C'},
                'daily': {'time': ['2030-01-01'], 'weather_code': [2], 'temperature_2m_min': [12],
                          'temperature_2m_max': [21], 'precipitation_probability_max': [20]}})
        plugin = self.make_plugin()
        plugin.client.transport = httpx.MockTransport(handle)
        result = await plugin.query_weather(city='西安', purpose='用户询问天气', historical_text='不得发送此内容')
        self.assertTrue(result['success'])
        second = await plugin.query_weather(city='西安')
        self.assertTrue(second['cached'])
        self.assertEqual(len(requests), 2)
        self.assertTrue(all('不得发送此内容' not in str(r.url) for r in requests))
        self.assertEqual(requests[0].url.params['name'], '西安')


if __name__ == '__main__':
    unittest.main()
