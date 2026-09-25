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
from heart_memory_backend import NativeBackend


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
        log = next((self.root / "logs").glob("*.txt")).read_text(encoding="utf-8-sig")
        self.assertIn("候选记忆", log)
        self.assertIn("测试同学喜欢Python", log)

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
        log = next((self.root / "logs").glob("*.txt")).read_text(encoding="utf-8-sig")
        self.assertIn("等待本人确认", log)
        self.assertIn("原生长期记忆已删除并复核", log)
        self.assertNotIn(HASH, log)

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
        log = next((self.root / "logs").glob("*.txt")).read_text(encoding="utf-8-sig")
        self.assertIn("自然语言已定位目标", log)
        self.assertIn("原生长期记忆已删除并复核", log)

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
        chat_manager = types.SimpleNamespace(last_messages={"private-1": message})
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
            self.assertIsNone(asyncio.run(NativeBackend().manage_actor("private-1")))

    def test_auto_decision_log_distinguishes_extraction_from_storage(self):
        self.store.append("自动记忆判断", "private-1", status="提取到候选",
                          memories=["测试同学喜欢Python"], evidence_message_ids=["msg-1"])
        text = next((self.root / "logs").glob("*.txt")).read_text(encoding="utf-8-sig")
        self.assertIn("原生AI记忆筛选", text)
        self.assertIn("尚不等于写入长期记忆", text)


if __name__ == "__main__":
    unittest.main()
