from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
import importlib.util
import sys
import unittest

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('qa_rt_adapter',ROOT/'vt/heart_realtime_client.py')
module=importlib.util.module_from_spec(spec);sys.modules[spec.name]=module;spec.loader.exec_module(module)

class AdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_closed_link_cancellation_never_calls_dead_plugin(self):
        link=SimpleNamespace(closed=True,epoch=2,socket=SimpleNamespace(closed=True),request=AsyncMock())
        await module.cancel_current({'link':link,'epoch':2})
        link.request.assert_not_awaited()
    async def test_old_turn_cancellation_does_not_stop_new_turn(self):
        link=SimpleNamespace(closed=False,epoch=3,socket=SimpleNamespace(closed=False),request=AsyncMock())
        await module.cancel_current({'link':link,'epoch':2})
        link.request.assert_not_awaited()
    async def test_current_turn_cancellation_really_requests_stop(self):
        link=SimpleNamespace(closed=False,epoch=2,socket=SimpleNamespace(closed=False),request=AsyncMock())
        await module.cancel_current({'link':link,'epoch':2})
        link.request.assert_awaited_once_with('stop')
    async def test_socket_closes_during_stop_is_expected_cancellation(self):
        link=SimpleNamespace(closed=False,epoch=2,socket=SimpleNamespace(closed=False))
        async def request(action):
            link.socket.closed=True
            raise ConnectionError('断开')
        link.request=request
        await module.cancel_current({'link':link,'epoch':2})
    async def test_live_connection_error_is_not_hidden(self):
        link=SimpleNamespace(closed=False,epoch=2,socket=SimpleNamespace(closed=False),
            request=AsyncMock(side_effect=RuntimeError('实际故障')))
        with self.assertRaises(RuntimeError):await module.cancel_current({'link':link,'epoch':2})

if __name__=='__main__':unittest.main(verbosity=2)
