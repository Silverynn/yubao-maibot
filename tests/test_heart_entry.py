"""新入口的离线测试：只用虚构人物，不触碰真实聊天、模型或记忆库。"""

import asyncio
import unittest
import uuid
import types
from unittest.mock import patch

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "extensions"))

from heart_shared.candidates import CandidateInbox
from heart_shared.forget import ForgetManager, NATURAL_FORGET_PATTERN, NATURAL_RESOLVE_PATTERN
import re
from heart_shared.storage import AuditStore
from heart_memory_backend import NativeBackend, before_memory_write, candidates_capability, group_person_hits, manage_capability


HASH = "a" * 64


class FakeBackend:
    def __init__(self):
        self.facts = [{"hash": HASH, "content": "测试同学喜欢Python"}]
        self.calls = []
        self.write_result = {"success": True, "stored_ids": ["new-id"]}
        self.natural_result = {"intent": True, "confidence": 0.97, "target_ids": [1]}

    async def owner(self, args):
        return {"person_id": "person-1", "name": "测试同学"}

    async def write_approved_candidate(self, args):
        self.calls.append(("write", args))
        return self.write_result

    def conflict_pending(self, chat_id, result):
        return "等待本人私聊确认" in str(result.get("detail") or "")

    async def person_facts(self, person_id):
        return self.facts if person_id == "person-1" else []

    async def match_natural_forget(self, actor, facts):
        self.calls.append(("match_natural_forget", actor["value"]))
        return self.natural_result

    async def invoke(self, component, args):
        self.calls.append((component, args))
        assert component == "memory_delete_admin"
        targets = [item for item in self.facts if item["hash"] in args["selector"]["hashes"]]
        if args["action"] == "preview":
            return {"success": True, "mode": "paragraph", "counts": {"paragraphs": len(targets)},
                    "items": [{"item_type": "paragraph", "item_hash": item["hash"],
                               "source": "person_fact:person-1", "preview": item["content"]}
                              for item in targets]}
        self.facts = [item for item in self.facts if item not in targets]
        return {"success": True, "deleted_paragraph_count": len(targets)}


class EntryTests(unittest.TestCase):
    def setUp(self):
        self.root = ROOT / ".runtime/test-data" / uuid.uuid4().hex
        self.store = AuditStore(self.root)
        self.backend = FakeBackend()

    @staticmethod
    def actor(action, value=""):
        return {"action": action, "value": value, "person_id": "person-1",
                "session_id": "private-1", "message_id": "msg-1"}

    def test_auto_candidate_not_written_until_confirmed_and_logged(self):
        inbox = CandidateInbox(self.store, self.backend)
        args = {"chat_id": "private-1", "text": "测试同学喜欢Python", "metadata": {"evidence_message_ids": ["msg-1"]}}
        result = asyncio.run(inbox.capture(args))
        self.assertFalse(result["success"])
        self.assertEqual(self.backend.calls, [])
        row = inbox.list_for("person-1")[0]
        self.assertIn("写入成功", asyncio.run(inbox.resolve("person-1", row["id"], True)))
        self.assertEqual(self.backend.calls[0][1]["metadata"]["fact_claim"]["trust"], "manual_confirmed")
        self.assertEqual(inbox.list_for("person-1"), [])
        log = next((self.root / "logs").rglob("*.txt")).read_text(encoding="utf-8-sig")
        self.assertIn("候选记忆", log)
        self.assertIn("测试同学喜欢Python", log)

    def test_same_evidence_candidate_spacing_variant_is_not_duplicated(self):
        inbox = CandidateInbox(self.store, self.backend)
        args = {"chat_id": "private-1", "text": "测试同学觉得 C++ 算法很难",
                "metadata": {"evidence_message_ids": ["msg-1"]}}
        asyncio.run(inbox.capture(args))
        result = asyncio.run(inbox.capture({**args, "text": "测试同学觉得C++算法很难"}))
        self.assertFalse(result["success"])
        self.assertIn("未重复写入", result["detail"])
        self.assertEqual(len(inbox.list_for("person-1")), 1)

    def test_auto_candidate_mode_skips_existing_semantic_duplicate(self):
        class Guard:
            async def check(self, args, config):
                return {"success": True, "stored_ids": [], "skipped_ids": [HASH],
                        "detail": "已有同义长期记忆，本次未重复写入"}

            def pending(self, chat):
                raise AssertionError("同义重复不需要进入冲突确认")

        class Inbox:
            async def capture(self, args, limit):
                raise AssertionError("已存同义事实不应再进入候选区")

        args = {"chat_id": "private-1", "text": "测试同学喜欢 Python",
                "source_type": "person_fact", "metadata": {"writeback_source": "memory_flow_service"}}
        config = {"auto_candidates_enabled": True, "auto_write_verified": False,
                  "max_pending_per_person": 20, "enabled": True}
        with patch("heart_memory_backend.settings", return_value=config), \
             patch("heart_memory_backend.guardian", return_value=Guard()), \
             patch("heart_memory_backend.candidate_inbox", return_value=Inbox()):
            result = asyncio.run(before_memory_write("ingest_text", args))
        self.assertEqual(result["skipped_ids"], [HASH])

    def test_candidate_conflict_still_requires_second_confirmation(self):
        inbox = CandidateInbox(self.store, self.backend)
        args = {"chat_id": "private-1", "text": "测试同学不再学Python", "metadata": {"evidence_message_ids": ["msg-2"]}}
        asyncio.run(inbox.capture(args))
        self.backend.write_result = {"success": False, "detail": "新旧信息疑似冲突，等待本人私聊确认后更新"}
        row = inbox.list_for("person-1")[0]
        self.assertIn("此时尚未写入", asyncio.run(inbox.resolve("person-1", row["id"], True)))
        self.assertEqual(inbox.list_for("person-1"), [])

    def test_forget_needs_private_confirmation_and_verifies_deletion(self):
        manager = ForgetManager(self.store, self.backend)
        list_text = asyncio.run(manager.handle(self.actor("list", "1")))
        self.assertIn("测试同学喜欢Python", list_text)
        proposal = asyncio.run(manager.handle(self.actor("forget", "喜欢Python")))
        self.assertIn("/确认忘记 1", proposal)
        self.assertEqual(self.backend.facts[0]["hash"], HASH)
        outcome = asyncio.run(manager.handle(self.actor("confirm", "1")))
        self.assertIn("已从可用的原生长期记忆中删除", outcome)
        self.assertEqual(self.backend.facts, [])
        self.assertIn("已经处理", asyncio.run(manager.handle(self.actor("confirm", "1"))))
        log = next((self.root / "logs").rglob("*.txt")).read_text(encoding="utf-8-sig")
        self.assertIn("等待本人确认", log)
        self.assertIn("原生长期记忆已删除并复核", log)
        self.assertNotIn(HASH, log)

    def test_forget_custom_threshold_and_confirmation_window(self):
        config = {"forget_min_confidence": 0.98, "forget_timeout_seconds": 5,
                  "forget_confirmation_minutes": 3, "forget_list_page_size": 5}
        manager = ForgetManager(self.store, self.backend, lambda: config)
        self.backend.natural_result["confidence"] = 0.97
        refused = asyncio.run(manager.handle(self.actor("natural_forget", "请忘掉我喜欢Python")))
        self.assertIn("没有改动", refused)
        self.assertEqual(self.backend.facts[0]["hash"], HASH)
        self.backend.natural_result["confidence"] = 0.99
        proposal = asyncio.run(manager.handle(self.actor("natural_forget", "请忘掉我喜欢Python")))
        self.assertIn("3分钟", proposal)
        with self.store.connect() as db:
            row = db.execute("SELECT expires FROM heart_forget_requests ORDER BY id DESC LIMIT 1").fetchone()
        import time
        self.assertLess(abs(row[0] - time.time() - 180), 5)

    def test_forget_ambiguous_and_wrong_owner_do_not_delete(self):
        self.backend.facts.append({"hash": "b" * 64, "content": "测试同学喜欢Python编程"})
        manager = ForgetManager(self.store, self.backend)
        self.assertIn("找到多条", asyncio.run(manager.handle(self.actor("forget", "喜欢Python"))))
        self.assertEqual(self.backend.calls, [])
        self.assertIn("没有找到属于你", asyncio.run(manager.handle({**self.actor("confirm", "1"), "person_id": "other"})))

    def test_preview_mismatch_blocks_delete(self):
        manager = ForgetManager(self.store, self.backend)
        with self.assertRaises(ValueError):
            manager.validate_preview({"success": True, "mode": "paragraph", "counts": {"paragraphs": 2},
                                      "items": [{"item_type": "paragraph", "item_hash": HASH,
                                                 "preview": "测试同学喜欢Python"}]}, self.backend.facts, "person-1")

    def test_natural_forget_requires_confirmation_and_removes_spacing_duplicates(self):
        self.backend.facts = [
            {"hash": HASH, "content": "测试同学觉得C++算法很难"},
            {"hash": "b" * 64, "content": "测试同学觉得 C++ 算法很难"},
        ]
        manager = ForgetManager(self.store, self.backend)
        phrase = "把我认为C++很难的记忆给我忘掉"
        self.assertTrue(re.fullmatch(NATURAL_FORGET_PATTERN, phrase))
        self.assertTrue(re.fullmatch(NATURAL_RESOLVE_PATTERN, "确认忘记"))
        proposal = asyncio.run(manager.handle(self.actor("natural_forget", phrase)))
        self.assertIn("以下2条长期记忆", proposal)
        self.assertEqual(len(self.backend.facts), 2)
        self.assertFalse(any(args.get("action") == "execute" for component, args in self.backend.calls
                             if component == "memory_delete_admin"))
        outcome = asyncio.run(manager.handle(self.actor("natural_confirm")))
        self.assertIn("删除2条", outcome)
        self.assertEqual(self.backend.facts, [])
        log = next((self.root / "logs").rglob("*.txt")).read_text(encoding="utf-8-sig")
        self.assertIn("自然语言已定位目标", log)
        self.assertIn("原生长期记忆已删除并复核", log)

    def test_group_story_about_someone_else_is_not_a_forget_command(self):
        story = "我记得某人是不是进入过某处数据库然后给管理员删掉了"
        self.assertIsNone(re.fullmatch(NATURAL_FORGET_PATTERN, story))
        self.assertIsNotNone(re.fullmatch(NATURAL_FORGET_PATTERN, "请你忘记我喜欢Python这条记忆"))

    def test_natural_forget_uncertain_or_cancelled_never_deletes(self):
        manager = ForgetManager(self.store, self.backend)
        self.backend.natural_result = {"intent": False, "confidence": 0.2, "target_ids": []}
        self.assertIn("不确定", asyncio.run(manager.handle(self.actor("natural_forget", "不要忘记我的记忆"))))
        self.assertFalse(any(component == "memory_delete_admin" for component, _ in self.backend.calls))
        self.backend.natural_result = {"intent": True, "confidence": 0.97, "target_ids": [1]}
        asyncio.run(manager.handle(self.actor("natural_forget", "把我的Python记忆忘掉")))
        self.assertIn("已取消", asyncio.run(manager.handle(self.actor("natural_cancel"))))
        self.assertEqual(len(self.backend.facts), 1)

    def test_natural_confirmation_rejects_wrong_owner(self):
        manager = ForgetManager(self.store, self.backend)
        asyncio.run(manager.handle(self.actor("natural_forget", "把我的Python记忆忘掉")))
        reply = asyncio.run(manager.handle({**self.actor("natural_confirm"), "person_id": "other"}))
        self.assertIn("没有改动记忆", reply)
        self.assertEqual(len(self.backend.facts), 1)

    def test_real_actor_adapter_routes_natural_phrase_and_confirmation(self):
        phrase = "把我认为C++很难的记忆给我忘掉"
        message = types.SimpleNamespace(
            session_id="private-1", processed_plain_text=phrase, platform="qq", message_id="msg-1",
            message_info=types.SimpleNamespace(
                group_info=None, user_info=types.SimpleNamespace(user_id="user-1")))
        session = types.SimpleNamespace(account_id="bot-1", scope="qq", user_id="user-1", group_id="")
        chat_manager = types.SimpleNamespace(last_messages={"private-1": message},
            get_existing_session_by_session_id=lambda _: session)
        fake = {
            name: types.ModuleType(name) for name in (
                "src", "src.chat", "src.chat.message_receive", "src.chat.message_receive.chat_manager",
                "src.chat.utils", "src.chat.utils.utils", "src.person_info", "src.person_info.person_info")}
        fake["src.chat.message_receive.chat_manager"].chat_manager = chat_manager
        fake["src.chat.utils.utils"].is_bot_self = lambda platform, user_id: False
        fake["src.person_info.person_info"].get_person_id = lambda platform, user_id: "person-1"
        with patch.dict(sys.modules, fake):
            actor = asyncio.run(NativeBackend().manage_actor("private-1"))
            self.assertEqual((actor["action"], actor["value"]), ("natural_forget", phrase))
            message.processed_plain_text = "确认忘记"
            self.assertEqual(asyncio.run(NativeBackend().manage_actor("private-1"))["action"], "natural_confirm")
            message.processed_plain_text = "取消忘记"
            self.assertEqual(asyncio.run(NativeBackend().manage_actor("private-1"))["action"], "natural_cancel")
            message.message_info.group_info = object()
            self.assertTrue(asyncio.run(NativeBackend().manage_actor("private-1"))["group"])
            self.assertIsNone(asyncio.run(NativeBackend().manage_actor("private-1", "older-command-id")))
            message.processed_plain_text = "我记得某人进数据库后给管理员删掉了"
            self.assertIsNone(asyncio.run(NativeBackend().manage_actor("private-1")))

    def test_group_memory_management_is_privately_delivered_and_group_confirmation_blocked(self):
        class GroupBackend:
            def __init__(self):
                self.sent = []

            async def manage_actor(self, session_id, expected_message_id=""):
                return {"action": "list", "value": "1", "group": True, "session_id": session_id,
                        "message_id": "msg-1", "person_id": "person-1"}

            async def private_session(self, actor):
                return "private-1"

            async def send(self, session, text):
                self.sent.append((session, text))
                return True

        backend = GroupBackend()
        class Manager:
            async def handle(self, actor):
                self_actor = actor
                assert self_actor["session_id"] == "private-1"
                return "本人私密事实：喜欢Python"
        with patch("heart_memory_backend.settings", return_value={"plugin_enabled": True}), \
             patch("heart_memory_backend.NativeBackend", return_value=backend), \
             patch("heart_memory_backend.forget_manager", return_value=Manager()):
            result = asyncio.run(manage_capability("heart.memory-audit", "", {"session_id": "group-1", "message_id": "msg-1"}))
            self.assertNotIn("喜欢Python", result["message"])
            self.assertIn("喜欢Python", backend.sent[0][1])
            self.assertEqual(backend.sent[0][0], "private-1")
            backend.manage_actor = lambda session_id, expected_message_id="": asyncio.sleep(0, result={
                "action": "confirm", "group": True, "session_id": session_id,
                "message_id": "msg-2", "person_id": "person-1"})
            blocked = asyncio.run(manage_capability("heart.memory-audit", "", {"session_id": "group-1", "message_id": "msg-2"}))
            self.assertIn("私聊中确认", blocked["message"])
            self.assertEqual(len(backend.sent), 1)

    def test_group_candidate_list_is_private_and_resolution_blocked(self):
        class GroupBackend:
            async def candidate_actor(self, session_id, expected_message_id=""):
                return {"action": "确认候选记忆", "id": 1, "group": True,
                        "session_id": session_id, "person_id": "person-1"}
        with patch("heart_memory_backend.settings", return_value={"plugin_enabled": True}), \
             patch("heart_memory_backend.NativeBackend", return_value=GroupBackend()):
            blocked = asyncio.run(candidates_capability("heart.memory-audit", "", {"session_id": "group-1", "message_id": "msg-1"}))
            self.assertIn("只能由本人在私聊", blocked["message"])

    def test_group_recall_queries_only_group_session_and_logs_hit_content(self):
        class GroupBackend:
            async def manage_actor(self, session_id, expected_message_id=""):
                return {"action": "group_recall", "value": "Python", "group": True,
                        "session_id": session_id, "message_id": "msg-1", "user_id": "user-1"}

            async def invoke(self, component, args):
                assert component == "search_memory"
                assert args["chat_id"] == "group-1" and args["group_id"] == "group-qq-1"
                assert args["user_id"] == "user-1" and args["person_id"] == ""
                assert args["respect_filter"] is True
                return {"success": True, "hits": [{"content": "本群曾讨论Python学习", "metadata": {"chat_id": "group-1"}}]}

        session = types.SimpleNamespace(group_id="group-qq-1")
        chat_module = types.ModuleType("src.chat.message_receive.chat_manager")
        chat_module.chat_manager = types.SimpleNamespace(get_existing_session_by_session_id=lambda _: session)
        modules = {name: types.ModuleType(name) for name in ("src", "src.chat", "src.chat.message_receive")}
        modules["src.chat.message_receive.chat_manager"] = chat_module
        with patch.dict(sys.modules, modules), \
             patch("heart_memory_backend.settings", return_value={"plugin_enabled": True}), \
             patch("heart_memory_backend.NativeBackend", return_value=GroupBackend()), \
             patch("heart_memory_backend.AuditStore", return_value=self.store):
            result = asyncio.run(manage_capability("heart.memory-audit", "", {"session_id": "group-1", "message_id": "msg-1"}))
        self.assertIn("本群曾讨论Python学习", result["message"])
        log = next((self.root / "logs").rglob("*.txt")).read_text(encoding="utf-8-sig")
        self.assertIn("本群曾讨论Python学习", log)
        self.assertNotIn("group-qq-1", log)

    def test_group_person_hits_never_falls_back_to_other_people_or_private_chats(self):
        hits = [
            {"content": "小甲在本群学习Python", "metadata": {"person_ids": ["person-a"], "chat_id": "group-1"}},
            {"content": "小甲在私聊学习C++", "metadata": {"person_ids": ["person-a"], "chat_id": "private-a"}},
            {"content": "小乙在本群学习Go", "metadata": {"person_ids": ["person-b"], "chat_id": "group-1"}},
            {"content": "文本写着小甲，但缺少归属证据", "metadata": {}},
        ]
        self.assertEqual([hit["content"] for hit in group_person_hits(hits, "person-a", "group-1")],
                         ["小甲在本群学习Python"])
        self.assertEqual(group_person_hits(hits, "person-c", "group-1"), [])

    def test_group_recall_uses_real_qq_at_component_not_typed_name(self):
        class AtComponent:
            def __init__(self, user_id, name):
                self.target_user_id, self.target_user_cardname = user_id, name
                self.target_user_nickname = name

        class TextComponent:
            def __init__(self, text):
                self.text = text

        message = types.SimpleNamespace(
            session_id="group-1", processed_plain_text="/群回忆 @小甲 Python", platform="qq", message_id="msg-1",
            raw_message=types.SimpleNamespace(components=[TextComponent("/群回忆 "), AtComponent("10001", "小甲"),
                                                          TextComponent(" Python")]),
            message_info=types.SimpleNamespace(group_info=object(), user_info=types.SimpleNamespace(user_id="writer")))
        session = types.SimpleNamespace(account_id="bot-1", scope="qq")
        chat_module = types.ModuleType("src.chat.message_receive.chat_manager")
        chat_module.chat_manager = types.SimpleNamespace(last_messages={"group-1": message},
            get_existing_session_by_session_id=lambda _: session)
        component_module = types.ModuleType("src.common.data_models.message_component_data_model")
        component_module.AtComponent, component_module.TextComponent = AtComponent, TextComponent
        modules = {name: types.ModuleType(name) for name in (
            "src", "src.chat", "src.chat.message_receive", "src.chat.utils", "src.chat.utils.utils",
            "src.person_info", "src.person_info.person_info", "src.common", "src.common.data_models")}
        modules["src.chat.message_receive.chat_manager"] = chat_module
        modules["src.common.data_models.message_component_data_model"] = component_module
        modules["src.chat.utils.utils"].is_bot_self = lambda platform, user_id: False
        modules["src.person_info.person_info"].get_person_id = lambda platform, user_id: f"person-{user_id}"
        with patch.dict(sys.modules, modules):
            actor = asyncio.run(NativeBackend().manage_actor("group-1", "msg-1"))
            self.assertEqual((actor["target_person_id"], actor["target_name"], actor["value"]),
                             ("person-10001", "小甲", "Python"))
            message.processed_plain_text = "/群回忆 @小甲"
            message.raw_message.components = [TextComponent("/群回忆 "), AtComponent("10001", "小甲")]
            self.assertEqual(asyncio.run(NativeBackend().manage_actor("group-1", "msg-1"))["value"], "小甲")
            message.processed_plain_text = "/群回忆 @小甲 @小乙"
            message.raw_message.components.append(AtComponent("10002", "小乙"))
            self.assertIn("一次只能@一位", asyncio.run(NativeBackend().manage_actor("group-1", "msg-1"))["target_error"])
            message.processed_plain_text = "/群回忆 @小甲"
            message.raw_message.components = [TextComponent("/群回忆 @小甲")]
            self.assertIn("真正的@", asyncio.run(NativeBackend().manage_actor("group-1", "msg-1"))["target_error"])

    def test_group_person_recall_filters_original_search_fallback_and_logs_person(self):
        class GroupBackend:
            async def manage_actor(self, session_id, expected_message_id=""):
                return {"action": "group_recall", "value": "Python", "group": True,
                        "session_id": session_id, "message_id": "msg-1", "user_id": "writer",
                        "target_person_id": "person-a", "target_name": "小甲"}

            async def invoke(self, component, args):
                assert args["person_id"] == "person-a" and args["mode"] == "aggregate"
                return {"success": True, "hits": [
                    {"content": "小乙在本群学习Go", "metadata": {"person_ids": ["person-b"], "chat_id": "group-1"}},
                    {"content": "小甲在私聊学习C++", "metadata": {"person_ids": ["person-a"], "chat_id": "private-a"}},
                    {"content": "小甲在本群学习Python", "metadata": {"person_ids": ["person-a"], "chat_id": "group-1"}},
                ]}

        session = types.SimpleNamespace(group_id="group-qq-1")
        chat_module = types.ModuleType("src.chat.message_receive.chat_manager")
        chat_module.chat_manager = types.SimpleNamespace(get_existing_session_by_session_id=lambda _: session)
        modules = {name: types.ModuleType(name) for name in ("src", "src.chat", "src.chat.message_receive")}
        modules["src.chat.message_receive.chat_manager"] = chat_module
        with patch.dict(sys.modules, modules), \
             patch("heart_memory_backend.settings", return_value={"plugin_enabled": True}), \
             patch("heart_memory_backend.NativeBackend", return_value=GroupBackend()), \
             patch("heart_memory_backend.AuditStore", return_value=self.store):
            result = asyncio.run(manage_capability("heart.memory-audit", "", {"session_id": "group-1", "message_id": "msg-1"}))
        self.assertIn("小甲在本群学习Python", result["message"])
        self.assertNotIn("私聊学习", result["message"])
        self.assertNotIn("小乙", result["message"])
        log = next((self.root / "logs").rglob("*.txt")).read_text(encoding="utf-8-sig")
        self.assertNotIn("小甲在私聊学习C++", log)

    def test_auto_decision_log_distinguishes_extraction_from_storage(self):
        self.store.append("自动记忆判断", "private-1", status="提取到候选",
                          memories=["测试同学喜欢Python"], evidence_message_ids=["msg-1"])
        text = next((self.root / "logs").rglob("*.txt")).read_text(encoding="utf-8-sig")
        self.assertIn("原生AI记忆筛选", text)
        self.assertIn("尚不等于写入长期记忆", text)


if __name__ == "__main__":
    unittest.main()
