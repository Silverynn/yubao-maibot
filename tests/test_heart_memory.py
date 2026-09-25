"""无 QQ、无付费模型的审计回归测试；所有数据都为虚构。"""

import asyncio
import importlib.util
import json
import sys
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
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
        next_day = self.store.clock() + timedelta(days=1)
        self.store.clock = lambda: next_day
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
        files = sorted((self.root / "logs").glob("*.txt"))
        self.assertEqual(len(files), 2)
        group_log = next(path.read_text(encoding="utf-8-sig") for path in files if "_群聊_测试群_" in path.name)
        private_log = next(path.read_text(encoding="utf-8-sig") for path in files if "_私聊_测试同学_" in path.name)
        self.assertIn("会话：群聊 测试群（心情按会话分别计算）", group_log)
        self.assertNotIn("与测试同学的会话", group_log)
        self.assertIn("会话：与测试同学的会话（心情按会话分别计算）", private_log)
        self.assertNotIn("群聊 测试群", private_log)
        self.assertNotIn("group-session", group_log + private_log)
        self.assertNotIn("personal-session", group_log + private_log)
        self.assertEqual(self.events()[-1]["dialogue"], [])
        self.assertEqual(self.events()[-1]["session_name"], "测试同学")

    def test_same_display_name_still_gets_separate_files_and_safe_filename(self):
        first = self.message(session="private-one", message_id="a", text="第一人的话")
        second = self.message(session="private-two", message_id="b", text="第二人的话")
        first["message_info"]["user_info"]["user_nickname"] = "小明:/\\?*"
        second["message_info"]["user_info"]["user_nickname"] = "小明:/\\?*"
        self.store.record_message(first)
        self.store.record_message(second)
        files = sorted((self.root / "logs").glob("*.txt"))
        self.assertEqual(len(files), 2)
        self.assertNotEqual(files[0].name, files[1].name)
        for path in files:
            self.assertIn("_私聊_小明", path.name)
            self.assertFalse(any(char in path.name for char in ':\\/?*'))
            text = path.read_text(encoding="utf-8-sig")
            self.assertNotEqual("第一人的话" in text, "第二人的话" in text)

    def test_three_day_retention_removes_database_and_files_without_resurrection(self):
        day = [datetime(2026, 9, value, 12, tzinfo=timezone.utc) for value in (21, 22, 25, 26)]
        self.store.clock = lambda: day[0]
        self.store.record_message(self.message(message_id="old", text="超过三天的对话"))
        with self.store.connect() as db:
            db.execute("INSERT INTO mood_processed VALUES(?,?,?)", ("s1", "old", '{"value":60}'))
            db.execute("INSERT INTO moods VALUES(?,?,?,?)", ("s1", 60, day[0].timestamp(), '{"value":60}'))
        untouched = self.root / "logs" / "my-notes.txt"
        untouched.write_text("用户自己的文件", encoding="utf-8")
        old_temp = self.root / "logs" / "2026-09-21_私聊_测试同学_aaaaaaaaaa.tmp"
        old_temp.write_text("旧临时日志", encoding="utf-8")
        self.store.clock = lambda: day[1]
        self.store.record_message(self.message(message_id="border", text="三天前的对话"))
        self.store.clock = lambda: day[2]
        self.store.record_message(self.message(message_id="now", text="今天的对话"))
        self.assertTrue(untouched.exists())
        self.assertFalse(old_temp.exists())
        files = list((self.root / "logs").glob("2026-*.txt"))
        self.assertEqual({path.name[:10] for path in files}, {"2026-09-22", "2026-09-25"})
        self.assertNotIn("超过三天的对话", json.dumps(self.events(), ensure_ascii=False))
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM mood_processed").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT value FROM moods WHERE session='s1'").fetchone()[0], 60)
        self.store.clock = lambda: day[3]
        self.store.prune()
        AuditStore(self.root, clock=lambda: day[3]).rebuild()
        files = list((self.root / "logs").glob("2026-*.txt"))
        self.assertEqual({path.name[:10] for path in files}, {"2026-09-25"})
        self.assertNotIn("三天前的对话", json.dumps(self.events(), ensure_ascii=False))
        self.assertEqual(untouched.read_text(encoding="utf-8"), "用户自己的文件")

    def test_rebuild_replaces_legacy_mixed_daily_file(self):
        self.store.record_message(self.message(session="one", message_id="a", text="甲会话"))
        self.store.record_message(self.message(session="two", message_id="b", text="乙会话"))
        day = self.store.clock().date().isoformat()
        legacy = self.root / "logs" / f"{day}.txt"
        legacy.write_text("旧版混合日志", encoding="utf-8")
        self.store.rebuild()
        self.assertFalse(legacy.exists())
        self.assertEqual(len(list((self.root / "logs").glob("*.txt"))), 2)

    def test_rebuild_names_legacy_group_session_from_old_message_row(self):
        now = self.store.clock()
        day = now.date().isoformat()
        with self.store.transaction() as db:
            db.execute("INSERT INTO messages VALUES(?,?,?,?,?,?)",
                       ("legacy-group", "old-1", "user", "旧群 / 老用户", "旧对话", now.isoformat()))
            db.execute("INSERT INTO events(event_id,day,time,session,kind,payload) VALUES(?,?,?,?,?,?)",
                       ("legacy-event", day, now.isoformat(), "legacy-group", "收到对话",
                        json.dumps({"text": "旧对话", "session_name": "旧群 / 老用户"}, ensure_ascii=False)))
        self.store.rebuild()
        files = list((self.root / "logs").glob(f"{day}_群聊_旧群_*.txt"))
        self.assertEqual(len(files), 1)
        self.assertIn("旧对话", files[0].read_text(encoding="utf-8-sig"))

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
        self.assertIn("候选待确认", plugin.memory_summary("ingest_text", {}, {"success": False,
            "detail": "已进入候选记忆 #3；尚未写入长期记忆"}, "")["status"])
        self.assertIn("冲突待确认", plugin.memory_summary("ingest_text", {}, {"success": False,
            "detail": "新旧信息疑似冲突，等待本人私聊确认后更新"}, "")["status"])
        self.store.append("记忆操作结果", "s1", operation="ingest_text", outcome=plugin.memory_summary(
            "ingest_text", {"text": "测试同学在学Python"}, {"success": False,
                "detail": "已进入候选记忆 #3；尚未写入长期记忆"}, ""))
        readable = next((self.root / "logs").glob("*.txt")).read_text(encoding="utf-8-sig")
        self.assertIn("结果：候选待确认，尚未写入长期记忆", readable)
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
