"""所有测试使用临时房间、假角色和模拟模型，不启动 MaiBot/QQ。"""
from pathlib import Path
from contextlib import closing
from unittest.mock import AsyncMock
import asyncio
import importlib.util
import json
import tempfile
import unittest

from fastapi.testclient import TestClient
from storyroom.app import create_app
from storyroom.store import RoomError, RoomStore

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("pc_worker", ROOT / "plugins/storyroom_pc/worker.py")
worker_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worker_module)


class PlatformTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="storyroom-tests-")
        self.addCleanup(self.temp.cleanup)
        self.client = TestClient(create_app(Path(self.temp.name), "isolated-host-token"))
        self.host = {"Authorization": "Bearer isolated-host-token"}
        result = self.client.post("/api/rooms", headers=self.host,
                                 json={"name": "测试房间"}).json()
        self.room, self.players = result["room_id"], result["connections"]
        self.pc1 = {"Authorization": "Bearer " + self.players[0]["token"]}
        self.pc2 = {"Authorization": "Bearer " + self.players[1]["token"]}
        self.prefix = "/api/rooms/" + self.room

    def snapshot(self):
        return self.client.get(self.prefix, headers=self.host).json()

    def request_task(self, player=0):
        result = self.client.post(self.prefix + "/turn", headers=self.host,
                json={"role": self.players[player]["id"], "question": "你想怎样做？"})
        self.assertEqual(result.status_code, 200)
        headers = self.pc1 if player == 0 else self.pc2
        return self.client.get("/api/bot/task", headers=headers).json()["task"]

    def propose(self, task, headers=None):
        data = {"lease": task["lease"], "speech": "我试着找齿轮", "action": "搜索桌子", "emotion": "疑惑"}
        return self.client.post("/api/bot/tasks/" + task["id"] + "/result",
                                headers=headers or self.pc1, json=data)

    def test_anonymous_and_wrong_key_cannot_read(self):
        self.assertEqual(self.client.get(self.prefix).status_code, 401)
        self.assertEqual(self.client.get(self.prefix, headers={"Authorization": "Bearer invalid"}).status_code, 401)

    def test_default_names_and_no_model_catalog(self):
        self.assertEqual([r['name'] for r in self.snapshot()['roles']], ['故机','鱼宝'])
        self.assertEqual([r['avatar'] for r in self.snapshot()['roles']], ['none','none'])

    def test_backend_defaults_choose_delivered_partner_model(self):
        vt=Path(self.temp.name)/'vt'
        vt.mkdir()
        (vt/'frontend/libs').mkdir(parents=True)
        names=['ds-whale-girl','moonbell-20261003','yubao-fullbody']
        entries=[]
        for name in names:
            file=vt/'live2d-models'/name/(name+'.model3.json')
            file.parent.mkdir(parents=True);file.write_text('{}',encoding='utf-8')
            entries.append({'name':name,'url':'/live2d-models/'+name+'/'+file.name})
        (vt/'model_dict.json').write_text(json.dumps(entries),encoding='utf-8')
        client=TestClient(create_app(Path(self.temp.name)/'catalog-rooms','isolated-host-token',vt))
        made=client.post('/api/rooms',headers=self.host,json={}).json()
        view=client.get('/api/rooms/'+made['room_id'],headers=self.host).json()
        self.assertEqual([r['avatar'] for r in view['roles']],['moonbell-20261003','yubao-fullbody'])

    def test_backend_defaults_work_with_partners_own_catalog(self):
        vt=Path(self.temp.name)/'other-vt';vt.mkdir()
        (vt/'frontend/libs').mkdir(parents=True)
        file=vt/'live2d-models/other/avatar.model3.json'
        file.parent.mkdir(parents=True);file.write_text('{}',encoding='utf-8')
        (vt/'model_dict.json').write_text(json.dumps([{'name':'伙伴自有模型','url':'/live2d-models/other/avatar.model3.json'}]),encoding='utf-8')
        client=TestClient(create_app(Path(self.temp.name)/'other-rooms','isolated-host-token',vt))
        made=client.post('/api/rooms',headers=self.host,json={}).json()
        view=client.get('/api/rooms/'+made['room_id'],headers=self.host).json()
        self.assertEqual([r['avatar'] for r in view['roles']],['伙伴自有模型','伙伴自有模型'])

    def test_windows_static_module_mime_type(self):
        response = self.client.get("/static/app.js")
        self.assertIn("text/javascript", response.headers["content-type"])
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")

    def test_player_cannot_act_as_host(self):
        self.assertEqual(self.client.post(self.prefix + "/advance", headers=self.pc1,
                                         json={"target": "workshop"}).status_code, 403)
        self.assertEqual(self.client.post("/api/rooms", headers=self.pc1, json={}).status_code, 403)

    def test_host_cannot_claim_pc_task(self):
        self.assertEqual(self.client.get("/api/bot/task", headers=self.host).status_code, 403)

    def test_room_isolation(self):
        other = self.client.post("/api/rooms", headers=self.host, json={}).json()["room_id"]
        self.assertEqual(self.client.get("/api/rooms/" + other, headers=self.pc1).status_code, 403)
        self.assertEqual(len(self.client.get("/api/rooms", headers=self.pc1).json()), 1)

    def test_role_does_not_leak_tokens(self):
        wire = json.dumps(self.snapshot())
        self.assertNotIn(self.players[0]["token"], wire)
        self.assertNotIn("key_hash", wire)
        with closing(sqlite(self.temp.name)) as db:
            self.assertNotEqual(db.execute("SELECT key_hash FROM roles LIMIT 1").fetchone()[0], self.players[0]["token"])

    def test_player_cannot_submit_someone_elses_task(self):
        task = self.request_task()
        self.assertEqual(self.propose(task, self.pc2).status_code, 404)

    def test_bad_lease_rejected(self):
        task = self.request_task()
        task["lease"] = "bad-lease-00000"
        self.assertEqual(self.propose(task).status_code, 403)

    def test_proposal_requires_host_confirmation(self):
        task = self.request_task()
        self.assertEqual(self.propose(task).status_code, 200)
        self.assertEqual(self.snapshot()["flags"], [])
        result = self.client.post(self.prefix + "/tasks/" + task["id"] + "/decide", headers=self.host,
            json={"ruling": "找到了备用齿轮", "flags": ["gear_found"], "trust_delta": 1})
        self.assertEqual(result.status_code, 200)
        snapshot = self.snapshot()
        self.assertEqual(snapshot["flags"], ["gear_found"])
        self.assertEqual(snapshot["roles"][0]["trust"], 1)
        self.assertTrue(any(e["kind"] == "主持人裁决" for e in snapshot["events"]))

    def test_duplicate_result_not_duplicate_event(self):
        task = self.request_task()
        self.propose(task)
        count = len(self.snapshot()["events"])
        self.assertTrue(self.propose(task).json()["duplicate"])
        self.assertEqual(len(self.snapshot()["events"]), count)

    def test_rejected_proposal_does_not_change_facts(self):
        task = self.request_task()
        self.propose(task)
        self.client.post(self.prefix + "/tasks/" + task["id"] + "/decide", headers=self.host,
            json={"approve": False, "ruling": "暂时没有发现", "flags": ["gear_found"], "trust_delta": 2})
        self.assertEqual(self.snapshot()["flags"], [])
        self.assertEqual(self.snapshot()["roles"][0]["trust"], 0)

    def test_extra_ai_fields_rejected(self):
        task = self.request_task()
        response = self.client.post("/api/bot/tasks/" + task["id"] + "/result", headers=self.pc1,
            json={"lease": task["lease"], "speech": "x", "action": "x", "emotion": "疑惑", "new_world": "异世界"})
        self.assertEqual(response.status_code, 422)

    def test_no_outside_world_transition_or_flag(self):
        self.assertEqual(self.client.post(self.prefix + "/advance", headers=self.host,
                                         json={"target": "new_universe"}).status_code, 409)
        task = self.request_task()
        self.propose(task)
        self.assertEqual(self.client.post(self.prefix + "/tasks/" + task["id"] + "/decide", headers=self.host,
            json={"ruling": "穿越", "flags": ["new_universe"]}).status_code, 409)

    def test_branch_requires_declared_evidence(self):
        self.client.post(self.prefix + "/advance", headers=self.host, json={"target": "tower"})
        self.assertEqual(self.client.post(self.prefix + "/advance", headers=self.host,
                                         json={"target": "light"}).status_code, 409)

    def test_finite_ending_stops_story(self):
        self.client.post(self.prefix + "/advance", headers=self.host, json={"target": "tower"})
        self.client.post(self.prefix + "/advance", headers=self.host, json={"target": "dark"})
        self.assertTrue(self.snapshot()["stage"]["ending"])
        self.assertEqual(self.client.post(self.prefix + "/narrate", headers=self.host,
                                         json={"text": "开始新故事"}).status_code, 409)
        self.assertEqual(self.client.post(self.prefix + "/turn", headers=self.host,
            json={"role": self.players[0]["id"]}).status_code, 409)

    def test_old_task_cancelled_after_narration(self):
        task = self.request_task()
        self.client.post(self.prefix + "/narrate", headers=self.host, json={"text": "现场已经改变"})
        self.assertEqual(self.propose(task).status_code, 409)
        self.assertEqual(self.snapshot()["tasks"][0]["status"], "cancelled")

    def test_old_proposal_can_be_cancelled_and_retried(self):
        task = self.request_task()
        self.propose(task)
        self.client.post(self.prefix + "/narrate", headers=self.host, json={"text": "门关上了"})
        self.assertEqual(self.client.post(self.prefix + "/tasks/" + task["id"] + "/decide", headers=self.host,
                                         json={"ruling": "继续"}).status_code, 409)
        self.assertEqual(self.client.post(self.prefix + "/tasks/" + task["id"] + "/cancel",
                                         headers=self.host, json={}).status_code, 200)
        self.request_task()

    def test_no_parallel_duplicate_turn_for_one_pc(self):
        self.request_task()
        result = self.client.post(self.prefix + "/turn", headers=self.host,
                                 json={"role": self.players[0]["id"]})
        self.assertEqual(result.status_code, 409)

    def test_real_dice_range_and_logs(self):
        result = self.client.post(self.prefix + "/dice", headers=self.host,
                                 json={"expression": "2d6+3", "dc": 9})
        data = result.json()
        self.assertEqual(len(data["每颗点数"]), 2)
        self.assertTrue(all(1 <= v <= 6 for v in data["每颗点数"]))
        self.assertEqual(data["总点数"], sum(data["每颗点数"]) + 3)
        self.assertTrue(any(e["kind"] == "投骰检定" for e in self.snapshot()["events"]))

    def test_invalid_dice_rejected(self):
        for expression in ("0d20", "30d6", "1d1000", "1d1", "exec(x)", "1d20+200"):
            self.assertGreaterEqual(self.client.post(self.prefix + "/dice", headers=self.host,
                                                    json={"expression": expression}).status_code, 400)

    def test_player_cannot_set_success_threshold(self):
        self.assertEqual(self.client.post(self.prefix + "/dice", headers=self.pc1,
                                         json={"dc": 1}).status_code, 403)

    def test_model_failure_is_not_a_proposal(self):
        task = self.request_task()
        self.client.post("/api/bot/tasks/" + task["id"] + "/result", headers=self.pc1,
            json={"lease": task["lease"], "speech": "", "action": "模型超时", "failed": True})
        self.assertEqual(self.snapshot()["tasks"][0]["status"], "failed")
        self.assertEqual(self.snapshot()["flags"], [])
        self.request_task()

    def test_key_rotation_revokes_old_key(self):
        result = self.client.post(self.prefix + "/roles/" + self.players[0]["id"] + "/rotate",
                                  headers=self.host, json={}).json()
        self.assertEqual(self.client.get(self.prefix, headers=self.pc1).status_code, 401)
        self.assertEqual(self.client.get(self.prefix, headers={"Authorization": "Bearer " + result["token"]}).status_code, 200)

    def test_cross_origin_request_rejected(self):
        self.assertEqual(self.client.post(self.prefix + "/dice",
            headers={**self.host, "Origin": "https://evil.example"}, json={}).status_code, 403)

    def test_http_cookie_is_httponly_strict(self):
        result = self.client.post("/api/login", json={"token": "isolated-host-token"})
        self.assertIn("HttpOnly", result.headers["set-cookie"])
        self.assertIn("SameSite=strict", result.headers["set-cookie"])
        self.assertEqual(self.client.get("/api/me").status_code, 200)

    def test_login_limit(self):
        for _ in range(8):
            self.assertEqual(self.client.post("/api/login", json={"token": "wrong-token"}).status_code, 401)
        self.assertEqual(self.client.post("/api/login", json={"token": "wrong-token"}).status_code, 429)

    def test_demo_is_explicit_and_costs_no_models(self):
        result = self.client.post(self.prefix + "/demo", headers=self.host, json={})
        self.assertEqual(result.json()["model_calls"], 0)
        snapshot = self.snapshot()
        self.assertEqual(len(snapshot["tasks"]), 2)
        self.assertTrue(all("离线演示" in t["result"]["speech"] for t in snapshot["tasks"]))
        self.assertEqual(snapshot["flags"], [])

    def test_visual_receipt_deduplicated(self):
        role = self.snapshot()["roles"][0]
        data = {"role": role["id"], "revision": role["visual"]["revision"], "client": "test-browser",
                "expression": "neutral", "label": "平静", "status": "已执行"}
        self.client.post(self.prefix + "/visual-receipt", headers=self.host, json=data)
        self.assertTrue(self.client.post(self.prefix + "/visual-receipt", headers=self.host, json=data).json()["duplicate"])

    def test_stale_receipt_ignored(self):
        role = self.snapshot()["roles"][0]
        data = {"role": role["id"], "revision": "old", "client": "test-browser",
                "expression": "neutral", "label": "平静", "status": "已执行"}
        self.assertIn("ignored", self.client.post(self.prefix + "/visual-receipt", headers=self.host, json=data).json())

    def test_readable_room_daily_log(self):
        self.client.post(self.prefix + "/dice", headers=self.host, json={})
        paths = list(Path(self.temp.name).glob("日志/*/*.txt"))
        self.assertEqual(len(paths), 1)
        text = paths[0].read_text(encoding="utf-8-sig")
        self.assertIn("投骰检定", text)
        self.assertIn("总点数", text)
        self.assertNotIn(self.players[0]["token"], text)

    def test_restart_keeps_worldline(self):
        self.client.post(self.prefix + "/advance", headers=self.host, json={"target": "workshop"})
        other = TestClient(create_app(Path(self.temp.name), "isolated-host-token"))
        self.assertEqual(other.get(self.prefix, headers=self.host).json()["node"], "workshop")

    def test_empty_proposal_rejected(self):
        task = self.request_task()
        response = self.client.post("/api/bot/tasks/" + task["id"] + "/result", headers=self.pc1,
            json={"lease": task["lease"], "speech": "", "action": "", "emotion": "平静"})
        self.assertEqual(response.status_code, 422)

    def test_lease_expiration_logged_and_old_lease_rejected(self):
        task = self.request_task()
        store = self.client.app.state.store
        now = store.clock()
        store.clock = lambda: now + 121
        replacement = self.client.get("/api/bot/task", headers=self.pc1).json()["task"]
        self.assertEqual(replacement["id"], task["id"])
        self.assertEqual(self.propose(task).status_code, 403)
        self.assertTrue(any(e["kind"] == "玩家任务等待超时" for e in self.snapshot()["events"]))
        self.assertEqual(self.propose(replacement).status_code, 200)

    def test_lease_retry_bound_does_not_run_forever(self):
        self.request_task()
        store = self.client.app.state.store
        now = store.clock()
        for seconds in (121, 242, 363):
            store.clock = lambda seconds=seconds: now + seconds
            response = self.client.get("/api/bot/task", headers=self.pc1).json()
        self.assertIsNone(response["task"])
        self.assertEqual(self.snapshot()["tasks"][0]["status"], "failed")

    def test_second_simultaneous_proposal_needs_new_world_context(self):
        first = self.request_task(0)
        second = self.request_task(1)
        self.assertIsNone(second)  # 0.3不允许同伴同时领取并抢话。
        self.propose(first)
        self.client.post(self.prefix + "/tasks/" + first["id"] + "/decide", headers=self.host,
                         json={"ruling": "第一个玩家已经发现齿轮", "flags": ["gear_found"]})
        response = self.client.get('/api/bot/task', headers=self.pc2).json()
        self.assertIsNone(response['task'])  # 排队期间现场变化，旧问题取消。
        next_task = self.request_task(1)
        self.assertIn('gear_found', next_task['flags'])


def sqlite(root):
    import sqlite3
    return sqlite3.connect(Path(root) / "rooms.sqlite3")


class WorkerTests(unittest.IsolatedAsyncioTestCase):
    def task(self):
        return {"id": "id", "lease": "sensitive-lease-not-for-llm", "question": "怎么办",
                "character_name": "测试角色", "world": "有限港口", "stage": {"title": "码头"},
                "flags": [], "trust": 0, "history": []}

    async def test_valid_pc_response_uploads_only_proposal(self):
        client = type("C", (), {"request": AsyncMock(side_effect=[{"task": self.task()}, {"ok": True}])})()
        generate = AsyncMock(return_value=json.dumps({"speech": "一起找", "action": "搜索", "emotion": "开心"}))
        worker = worker_module.PCWorker(client, generate, "测试角色", "", 1, lambda x: None)
        self.assertTrue(await worker.once())
        self.assertEqual(generate.await_count, 1)
        self.assertFalse(client.request.await_args_list[1].args[1]["failed"])
        self.assertNotIn("sensitive-lease", generate.await_args.args[0])

    async def test_invalid_json_visible_failure(self):
        client = type("C", (), {"request": AsyncMock(side_effect=[{"task": self.task()}, {}])})()
        worker = worker_module.PCWorker(client, AsyncMock(return_value="已经拯救世界"), "", "", 1, lambda x: None)
        await worker.once()
        self.assertTrue(client.request.await_args_list[-1].args[1]["failed"])

    async def test_timeout_reported_without_retries_of_model(self):
        async def slow(_):
            await asyncio.sleep(.2)
        client = type("C", (), {"request": AsyncMock(side_effect=[{"task": self.task()}, {}])})()
        worker = worker_module.PCWorker(client, slow, "", "", .01, lambda x: None)
        await worker.once()
        self.assertIn("TimeoutError", client.request.await_args_list[-1].args[1]["action"])

    async def test_no_task_no_model_call(self):
        client = type("C", (), {"request": AsyncMock(return_value={"task": None})})()
        model = AsyncMock()
        worker = worker_module.PCWorker(client, model, "", "", 1, lambda x: None)
        self.assertFalse(await worker.once())
        model.assert_not_called()

    async def test_poll_cancellation_propagates(self):
        client = type("C", (), {"request": AsyncMock(side_effect=asyncio.CancelledError)})()
        worker = worker_module.PCWorker(client, AsyncMock(), "", "", 1, lambda x: None)
        with self.assertRaises(asyncio.CancelledError):
            await worker.once()

    def test_remote_cleartext_and_redirect_policy(self):
        with self.assertRaises(ValueError):
            worker_module.RoomClient("http://example.org:8790", "key")
        with self.assertRaises(ValueError):
            worker_module.RoomClient("https://user:password@example.org/", "key")
        self.assertIsNone(worker_module.NoRedirect().redirect_request(None, None, 302, "", {}, "https://evil.org"))

    def test_reply_shape_and_emotion_constraints(self):
        for text in ('[]', '{"speech":"x","action":"y","emotion":"无敌"}', '{"speech":"","action":"","emotion":"平静"}'):
            with self.assertRaises(ValueError):
                worker_module.parse_reply(text)


if __name__ == "__main__":
    unittest.main()
