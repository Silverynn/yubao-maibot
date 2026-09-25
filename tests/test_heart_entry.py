"""新入口的离线测试：只用虚构人物，不触碰真实聊天、模型或记忆库。"""

import asyncio
import unittest
import uuid

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "extensions"))

from heart_shared.candidates import CandidateInbox
from heart_shared.forget import ForgetManager
from heart_shared.storage import AuditStore


HASH = "a" * 64


class FakeBackend:
    def __init__(self):
        self.facts = [{"hash": HASH, "content": "测试同学喜欢Python"}]
        self.calls = []
        self.write_result = {"success": True, "stored_ids": ["new-id"]}

    async def owner(self, args):
        return {"person_id": "person-1", "name": "测试同学"}

    async def write_approved_candidate(self, args):
        self.calls.append(("write", args))
        return self.write_result

    def conflict_pending(self, chat_id, result):
        return "等待本人私聊确认" in str(result.get("detail") or "")

    async def person_facts(self, person_id):
        return self.facts if person_id == "person-1" else []

    async def invoke(self, component, args):
        self.calls.append((component, args))
        assert component == "memory_delete_admin"
        if args["action"] == "preview":
            return {"success": True, "mode": "paragraph", "counts": {"paragraphs": 1},
                    "items": [{"item_type": "paragraph", "item_hash": HASH,
                               "source": "person_fact:person-1", "preview": "测试同学喜欢Python"}]}
        self.facts = []
        return {"success": True, "deleted_paragraph_count": 1}


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
                                                 "preview": "测试同学喜欢Python"}]}, self.backend.facts[0], "person-1")

    def test_auto_decision_log_distinguishes_extraction_from_storage(self):
        self.store.append("自动记忆判断", "private-1", status="提取到候选",
                          memories=["测试同学喜欢Python"], evidence_message_ids=["msg-1"])
        text = next((self.root / "logs").glob("*.txt")).read_text(encoding="utf-8-sig")
        self.assertIn("原生AI记忆筛选", text)
        self.assertIn("尚不等于写入长期记忆", text)


if __name__ == "__main__":
    unittest.main()
