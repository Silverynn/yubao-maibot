"""实际 HTTP 通路：两个独立客户端共享一个平台。模型使用明确标记的模拟函数。"""
from pathlib import Path
from unittest.mock import AsyncMock
import asyncio
import importlib.util
import json
import socket
import tempfile
import threading
import time
import unittest
import uvicorn

from storyroom.app import create_app


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("network_pc", ROOT / "plugins/storyroom_pc/worker.py")
pc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pc)


class NetworkTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="storyroom-http-")
        self.socket = socket.socket()
        self.socket.bind(("127.0.0.1", 0))
        self.socket.listen(128)
        self.socket.setblocking(False)
        address = "http://127.0.0.1:" + str(self.socket.getsockname()[1])
        self.host = pc.RoomClient(address, "isolated-network-host")
        self.app = create_app(Path(self.temp.name), "isolated-network-host")
        self.server = uvicorn.Server(uvicorn.Config(self.app, log_level="error", access_log=False))
        self.thread = threading.Thread(target=lambda: self.server.run(sockets=[self.socket]), daemon=True)
        self.thread.start()
        limit = time.monotonic() + 5
        while not self.server.started:
            if time.monotonic() >= limit:
                raise RuntimeError("隔离测试服务未能启动")
            time.sleep(.01)
        result = self.host.sync_request("/api/rooms", {"name": "两个独立HTTP客户端"})
        self.room, self.roles = result["room_id"], result["connections"]
        self.clients = [pc.RoomClient(address, role["token"]) for role in self.roles]
        self.prefix = "/api/rooms/" + self.room

    def tearDown(self):
        self.server.should_exit = True
        self.thread.join(timeout=5)
        self.socket.close()
        self.temp.cleanup()
        self.assertFalse(self.thread.is_alive())

    async def test_two_pc_clients_approval_dice_and_shared_context(self):
        models = [AsyncMock(return_value=json.dumps({"speech": "【模拟模型】找零件",
                  "action": "搜索桌子", "emotion": emotion}, ensure_ascii=False)) for emotion in ("疑惑", "开心")]
        workers = [pc.PCWorker(client, model, "测试角色", "", 2, lambda x: None)
                   for client, model in zip(self.clients, models)]
        for role in self.roles:
            await self.host.request(self.prefix + "/turn", {"role": role["id"]})
        self.assertEqual(await asyncio.gather(*(worker.once() for worker in workers)), [True, False])
        view = await self.host.request(self.prefix)
        self.assertEqual([t["status"] for t in view["tasks"]], ["queued", "proposed"])
        self.assertEqual(view["flags"], [])
        self.assertTrue(all(r["connected"] for r in view["roles"]))
        first = next(t for t in view["tasks"] if t["role"] == self.roles[0]["id"])
        second = next(t for t in view["tasks"] if t["role"] == self.roles[1]["id"])
        await self.host.request(self.prefix + "/tasks/" + first["id"] + "/decide",
                                {"ruling": "模拟测试：确认找到了齿轮", "flags": ["gear_found"], "trust_delta": 1})
        await self.host.request(self.prefix + "/tasks/" + second["id"] + "/cancel", {})
        await self.host.request(self.prefix + "/turn", {"role": self.roles[1]["id"]})
        self.assertTrue(await workers[1].once())
        self.assertIn("gear_found", models[1].await_args.args[0])
        self.assertIn("确认找到了齿轮", models[1].await_args.args[0])
        dice = await self.clients[1].request(self.prefix + "/dice", {"expression": "2d6+3"})
        self.assertTrue(5 <= dice["总点数"] <= 15)
        self.assertEqual(models[0].await_count, 1)
        self.assertEqual(models[1].await_count, 1)

    async def test_actual_http_cancel_during_generation(self):
        started, cancelled = asyncio.Event(), asyncio.Event()
        async def generate(_):
            started.set()
            try:
                await asyncio.sleep(30)
            finally:
                cancelled.set()
        receipt = await self.host.request(self.prefix+'/turn', {'role': self.roles[0]['id']})
        worker = pc.PCWorker(self.clients[0], generate, '测试角色', '', 10, lambda _: None,
                             data_dir=Path(self.temp.name)/'pc-private')
        job = asyncio.create_task(worker.once())
        await asyncio.wait_for(started.wait(), 3)
        await self.host.request(self.prefix+'/tasks/'+receipt['task_id']+'/cancel', {})
        self.assertFalse(await asyncio.wait_for(job, 4))
        self.assertTrue(cancelled.is_set())
        state = await self.host.request(self.prefix)
        self.assertEqual(state['tasks'][0]['status'], 'rejected')
        self.assertIsNone(state['tasks'][0]['result'])
        self.assertEqual(state['experiences'], [])

    async def test_continuous_observation_while_model_thinks_and_after_restart(self):
        started, finish = asyncio.Event(), asyncio.Event()
        async def generate(_):
            started.set()
            await finish.wait()
            return json.dumps({'speech':'【模拟模型】门外可能有船，伙伴你觉得呢？','action':'','emotion':'疑惑','intent':'discussion'}, ensure_ascii=False)
        log = pc.Audit(Path(self.temp.name)/'observer-log')
        worker = pc.PCWorker(self.clients[0],generate,'测试角色','',10,lambda _:None, interval=.05,
                             data_dir=Path(self.temp.name)/'observer-pc',audit=log)
        await self.host.request(self.prefix+'/talk',{'opened':True,'limit':2})
        job=asyncio.create_task(worker.run())
        try:
            await asyncio.wait_for(started.wait(),4)
            await self.clients[1].request(self.prefix+'/dialogue',{'text':'同伴在思考时，我补充：门外有钟声'})
            deadline=time.monotonic()+4
            while time.monotonic()<deadline:
                logs=''.join(p.read_text(encoding='utf-8') for p in (Path(self.temp.name)/'observer-log').glob('*.jsonl'))
                if '门外有钟声' in logs:break
                await asyncio.sleep(.05)
            self.assertIn('门外有钟声',logs)
            self.assertFalse(finish.is_set())  # 模型仍在思考，旁听已经同步，未新增模型调用。
            finish.set()
            await asyncio.sleep(.2)
        finally:
            job.cancel();await asyncio.gather(job,return_exceptions=True)
