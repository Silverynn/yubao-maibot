"""无 QQ、无付费模型的审计回归测试；所有数据都为虚构。"""

import asyncio
import importlib.util
import json
import sys
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "extensions"))
from heart_shared.storage import AuditStore


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


plugin = load("test_memory_plugin", ROOT / "plugins/heart_memory_audit/plugin.py")
bridge = load("test_memory_bridge", ROOT / "extensions/heart_host_observer.py")


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.root = ROOT / ".runtime/test-data" / uuid.uuid4().hex
        self.store = AuditStore(self.root)

    def message(self, session="s1", message_id="m1", text="我在学Python"):
        return {"session_id": session, "message_id": message_id, "processed_plain_text": text,
                "message_info": {"user_info": {"user_id": "synthetic-user", "user_nickname": "测试同学"}}}

    def events(self):
        with self.store.connect() as db:
            return [json.loads(row[0]) for row in db.execute("SELECT payload FROM events ORDER BY seq")]

    def test_daily_unicode_and_idempotent_message(self):
        self.store.record_message(self.message())
        self.store.record_message(self.message())
        self.assertEqual(len(self.events()), 1)
        self.store.append("test", "s1", message_id="m1", value="中文")
        content = next((self.root / "logs").glob("*.txt")).read_text(encoding="utf-8-sig")
        self.assertIn("\n\n#", content)
        self.assertIn("我在学Python", content)
        self.store.clock = lambda: datetime(2030, 1, 1, tzinfo=timezone.utc)
        self.store.append("test", "s1")
        self.assertEqual(len(list((self.root / "logs").glob("*.txt"))), 2)

    def test_no_cross_session_and_no_false_association(self):
        self.store.record_message(self.message())
        self.store.append("写入", "s2", evidence_message_ids=["m1"])
        self.assertEqual(self.events()[-1]["dialogue"], [])
        self.store.append("检索", "s1")
        self.assertIn("不代表", self.events()[-1]["relation"])

    def test_readable_log_distinguishes_group_and_personal_mood(self):
        group = self.message(session="group-session", message_id="g1")
        group["message_info"]["group_info"] = {"group_name": "测试群"}
        self.store.record_message(group)
        self.store.record_message(self.message(session="personal-session", message_id="p1"))
        self.store.append("模型请求中的记忆参考", "personal-session", references=[])
        self.store.append("记忆操作结果", "personal-session", evidence_message_ids=["unknown"],
                          operation="get_person_profile", outcome={"result": {}})
        content = next((self.root / "logs").glob("*.txt")).read_text(encoding="utf-8-sig")
        self.assertIn("会话：群聊 测试群（心情按会话分别计算）", content)
        self.assertIn("会话：与测试同学的会话（心情按会话分别计算）", content)
        self.assertNotIn("group-session", content)
        self.assertNotIn("personal-session", content)
        self.assertEqual(self.events()[-1]["dialogue"], [])
        self.assertEqual(self.events()[-1]["session_name"], "测试同学")

    def test_concurrent_and_restart(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda i: self.store.append("test", "s1", number=i), range(20)))
        AuditStore(self.root).rebuild()
        self.assertEqual(len(self.events()), 20)
        text = next((self.root / "logs").glob("*.txt")).read_text(encoding="utf-8-sig")
        self.assertIn("#20", text)

    def test_result_status_and_redaction(self):
        self.assertIn("跳过", plugin.memory_summary("ingest_text", {}, {"skipped_ids": ["x"]}, "")["status"])
        self.assertIn("不能认定", plugin.memory_summary("ingest_text", {}, {"success": True}, "")["status"])
        self.assertEqual(plugin.memory_summary("search_memory", {}, {}, "TimeoutError")["status"], "调用失败")
        self.store.append("test", "s1", api_key="no-leak", text="Bearer xyz123")
        self.assertNotIn("xyz123", json.dumps(self.events()))
        self.assertNotIn("no-leak", json.dumps(self.events()))

    def test_readable_log_hides_internal_fields_but_keeps_database(self):
        long_id = "a" * 64
        self.store.record_message(self.message(session=long_id))
        self.store.append("记忆操作结果", long_id, operation="search_memory", duration_ms=123,
            outcome={"status": "检索完成", "query": "学习", "hits": [{"hash": long_id, "score": 1.1,
                "content": "测试同学喜欢学习Python", "metadata": {"person_id": long_id}}]})
        text = next((self.root / "logs").glob("*.txt")).read_text(encoding="utf-8-sig")
        for hidden in (long_id, "incoming:", "metadata", "person_id", "score", "search_memory"):
            self.assertNotIn(hidden, text)
        self.assertIn("测试同学喜欢学习Python", text)
        self.assertIn("人物：测试同学", text)
        self.assertIn("检索返回 1 条候选", text)
        self.assertEqual(self.events()[-1]["outcome"]["hits"][0]["hash"], long_id)

    def test_readable_failed_write_not_success(self):
        self.store.append("记忆操作结果", "s1", operation="ingest_text",
            outcome={"status": "调用失败", "error": "TimeoutError: " + "b" * 64})
        text = next((self.root / "logs").glob("*.txt")).read_text(encoding="utf-8-sig")
        self.assertIn("结果：未成功 · 等待超时", text)
        self.assertNotIn("b" * 64, text)
        self.assertNotIn("已存储", text)

    def test_real_hook_handlers(self):
        async def run():
            instance = plugin.create_plugin()
            instance.set_plugin_config({})
            instance.store = self.store
            await instance.incoming(message=self.message())
            await instance.memory(event={"event_id": "write-1", "component": "ingest_text", "duration_ms": 1,
                "arguments": {"chat_id": "s1", "text": "学习Python", "metadata": {"evidence_message_ids": ["m1"]}},
                "result": {"stored_ids": ["fact-1"]}, "error": ""})
            item = {"type": "message", "content": [{"type": "text", "text": "【长期记忆检索结果-内部参考】\n学习Python"}]}
            before = json.dumps(item)
            await instance.reply_prompt(session_id="s1", reply_message_id="m1", items=[item])
            self.assertEqual(json.dumps(item), before)
            await instance.sent(message=self.message(), sent=False, reply_message_id="m1")
        asyncio.run(run())
        self.assertEqual(self.events()[1]["outcome"]["stored_ids"], ["fact-1"])
        self.assertTrue(self.events()[2]["references"])
        self.assertFalse(self.events()[3]["sent"])

    def test_bridge_preserves_return_and_errors(self):
        seen = []
        async def notify(event):
            seen.append(event)
        original = bridge.notify
        bridge.notify = notify
        obj = {"stored_ids": ["x"]}
        class Fake:
            @bridge.observed_memory_call
            async def call(self, name, args=None, **kwargs):
                if name == "bad":
                    raise ValueError("synthetic")
                return obj
        async def run():
            self.assertIs(await Fake().call("ingest_text"), obj)
            with self.assertRaises(ValueError):
                await Fake().call("bad")
        try:
            asyncio.run(run())
        finally:
            bridge.notify = original
        self.assertEqual(len(seen), 2)
        self.assertIn("ValueError", seen[1]["error"])


if __name__ == "__main__":
    unittest.main()
