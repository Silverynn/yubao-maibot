"""群记忆链路回归：临时 SQLite、虚构人物、模拟模型/原生写入，不连接 QQ。"""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import json
import unittest
import uuid

from heart_shared.candidates import CandidateInbox
from heart_shared.storage import AuditStore
from src.services import heart_host_observer, heart_memory_backend, heart_memory_scope, memory_flow_service
from pytests.plugin_runtime.test_heart_group_recall_scope import _Kernel, _Store


class GroupCandidateRoundtripTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # 保留虚构测试产物供排查；不在 Windows 沙箱中改 ACL 或递归清理临时目录。
        test_root = Path(__file__).resolve().parents[2] / ".runtime" / "test-data" / uuid.uuid4().hex
        self.audit = AuditStore(test_root)
        self.native = _Store()
        self.addCleanup(self.native.db.close)
        self.args = {
            "chat_id": "group-1", "text": "小明喜欢围棋", "source_type": "person_fact",
            "person_ids": ["person-1"],
            "metadata": {"evidence_message_ids": ["group-message-1"],
                         "writeback_source": "memory_flow_service"},
        }
        self.writes = []

        async def write(args):
            self.writes.append(args)
            self.native.add(args["text"], "person_fact:person-1", args["chat_id"], 10,
                            person_id="person-1")
            return {"success": True, "stored_ids": ["test-id"]}

        self.backend = SimpleNamespace(
            owner=AsyncMock(return_value={"person_id": "person-1", "name": "小明"}),
            write_approved_candidate=write,
            conflict_pending=lambda chat, result: bool(result.get("pending")),
        )
        self.inbox = CandidateInbox(self.audit, self.backend)

    async def recall(self):
        with (patch.object(heart_memory_scope, "kernel_for_read", AsyncMock(return_value=_Kernel(self.native))),
              patch.object(heart_memory_scope, "session_for", return_value=SimpleNamespace(group_id="qq-group-1"))):
            return await heart_memory_scope.prepare_read("heart_scoped_list", {
                "chat_id": "group-1", "limit": 5, "include_group_summaries": True,
            })

    async def test_candidate_confirmation_keeps_original_group_and_logs(self):
        await self.inbox.capture(self.args)
        self.assertEqual((await self.recall())["hits"], [])  # 待审核不冒充长期记忆。
        candidate_id = self.inbox.list_for("person-1")[0]["id"]
        self.assertIn("写入成功", await self.inbox.resolve("person-1", candidate_id, True))
        self.assertEqual(self.writes[0]["chat_id"], "group-1")  # 私聊确认不改成私聊来源。
        self.assertEqual(self.writes[0]["metadata"]["fact_claim"]["trust"], "manual_confirmed")
        self.assertEqual([h["content"] for h in (await self.recall())["hits"]], ["小明喜欢围棋"])
        with self.audit.connect() as db:
            events = db.execute("SELECT session,payload FROM events WHERE kind='候选记忆' ORDER BY seq").fetchall()
        self.assertEqual([e["session"] for e in events], ["group-1", "group-1"])
        self.assertEqual(json.loads(events[-1]["payload"])["status"], "written")
        text = "\n".join(path.read_text(encoding="utf-8-sig") for path in (self.audit.root / "logs").rglob("*.txt"))
        self.assertIn("小明喜欢围棋", text)
        self.assertIn("写入成功", text)

    async def test_other_person_cannot_confirm_and_ignore_does_not_write(self):
        await self.inbox.capture(self.args)
        candidate_id = self.inbox.list_for("person-1")[0]["id"]
        self.assertIn("没有找到属于你", await self.inbox.resolve("person-2", candidate_id, True))
        self.assertIn("已忽略", await self.inbox.resolve("person-1", candidate_id, False))
        self.assertEqual(self.writes, [])
        self.assertEqual((await self.recall())["hits"], [])

    async def test_candidate_approval_does_not_bypass_conflict_confirmation(self):
        self.backend.write_approved_candidate = AsyncMock(return_value={"success": False, "pending": True})
        await self.inbox.capture(self.args)
        candidate_id = self.inbox.list_for("person-1")[0]["id"]
        self.assertIn("与旧记忆可能冲突", await self.inbox.resolve("person-1", candidate_id, True))
        self.assertEqual((await self.recall())["hits"], [])
        with self.audit.connect() as db:
            row = db.execute("SELECT status FROM heart_candidates WHERE id=?", (candidate_id,)).fetchone()
        self.assertEqual(row["status"], "conflict_pending")

    async def test_group_command_returns_current_group_and_records_exact_hits(self):
        self.native.add("本群讨论了围棋", "chat_summary:group-1", "group-1", 5)
        self.native.add("小明的私聊内容", "person_fact:person-1", "private-1", 100, person_id="person-1")
        self.native.add("别的群的内容", "chat_summary:group-2", "group-2", 101)
        actor = {"session_id": "group-1", "message_id": "request-1", "group": True,
                 "action": "group_recall", "value": "", "user_id": "test-user"}

        async def invoke(component, args):
            with (patch.object(heart_memory_scope, "kernel_for_read", AsyncMock(return_value=_Kernel(self.native))),
                  patch.object(heart_memory_scope, "session_for", return_value=SimpleNamespace(group_id="qq-group-1"))):
                return await heart_memory_scope.prepare_read(component, args)

        session = SimpleNamespace(group_id="qq-group-1")
        from src.chat.message_receive.chat_manager import chat_manager
        with (patch.object(heart_memory_backend.NativeBackend, "manage_actor", AsyncMock(return_value=actor)),
              patch.object(heart_memory_backend.NativeBackend, "invoke", side_effect=invoke),
              patch.object(heart_memory_backend, "AuditStore", return_value=self.audit),
              patch.object(heart_memory_backend, "settings", return_value={"plugin_enabled": True}),
              patch.object(chat_manager, "get_existing_session_by_session_id", return_value=session)):
            result = await heart_memory_backend.manage_capability("heart.memory-audit", "heart.memory.manage", {
                "session_id": "group-1", "message_id": "request-1",
            })
        self.assertTrue(result["success"])
        self.assertIn("本群讨论了围棋", result["message"])
        self.assertNotIn("小明的私聊内容", result["message"])
        self.assertNotIn("别的群的内容", result["message"])
        with self.audit.connect() as db:
            event = json.loads(db.execute("SELECT payload FROM events WHERE kind='群聊记忆检索'").fetchone()[0])
        self.assertEqual(event["hits"], ["本群讨论了围棋"])
        self.assertEqual(event["hit_types"], ["本群摘要"])

    async def test_write_observer_records_body_checked_from_storage(self):
        @heart_host_observer.observed_memory_call
        async def native_write(_self, component, args):
            self.native.add("实际落库的群聊摘要", "chat_summary:group-1", args["chat_id"], 10)
            return {"success": True, "stored_ids": ["test-summary"]}

        self.native.get_paragraph = lambda key: (self.native.get_paragraphs_by_source("chat_summary:group-1")[0]
                                               if key == "test-summary" else None)
        with (patch.object(heart_memory_backend, "before_memory_write", AsyncMock(return_value=None)),
              patch.object(heart_memory_backend, "settings", return_value={"plugin_enabled": True}),
              patch.object(heart_host_observer, "audit_store", return_value=self.audit),
              patch.object(heart_memory_scope, "kernel_for_read", AsyncMock(return_value=_Kernel(self.native)))):
            result = await native_write(None, "ingest_summary", {"chat_id": "group-1", "text": "待总结原文"})
        self.assertNotIn("_heart_stored_contents", result)  # 日志补充信息不改变原生返回。
        with self.audit.connect() as db:
            payload = json.loads(db.execute("SELECT payload FROM events WHERE kind='记忆操作结果'").fetchone()[0])
        self.assertEqual(payload["operation"], "ingest_summary")
        self.assertEqual(payload["outcome"]["stored_contents"], ["实际落库的群聊摘要"])
        self.assertEqual(payload["outcome"]["submitted_text"], "待总结原文")


class GroupAutomaticWritebackTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_person_fact_forwarding_keeps_group_and_evidence(self):
        service = memory_flow_service.PersonFactWritebackService()
        person = SimpleNamespace(is_known=True, person_id="person-1", person_name="小明")
        evidence = SimpleNamespace(target_messages=[SimpleNamespace(message_id="group-source-1")])
        message = SimpleNamespace(processed_plain_text="围棋确实很有意思，下次可以继续聊。", session_id="group-1")
        write, audit = AsyncMock(), AsyncMock()
        with (patch.object(service, "_resolve_target_person", return_value=person),
              patch.object(service, "_collect_user_evidence", return_value=evidence),
              patch.object(service, "_format_user_evidence", return_value="我喜欢围棋"),
              patch.object(service, "_extract_facts", AsyncMock(return_value=["小明喜欢围棋"])),
              patch.object(memory_flow_service, "store_person_memory_from_answer", write),
              patch.object(heart_memory_backend, "record_auto_selection", audit)):
            await service._handle_message(message)
        self.assertEqual(write.await_args.args, ("小明", "小明喜欢围棋", "group-1"))
        self.assertEqual(write.await_args.kwargs["evidence_message_ids"], ["group-source-1"])
        self.assertEqual(audit.await_args.args, ("group-1", ["小明喜欢围棋"], ["group-source-1"]))

    async def test_group_summary_not_blocked_by_pending_person_fact(self):
        guard = SimpleNamespace(check=AsyncMock(), pending=lambda chat: True)
        with (patch.object(heart_memory_backend, "settings", return_value={"enabled": True}),
              patch.object(heart_memory_backend, "guardian", return_value=guard)):
            result = await heart_memory_backend.before_memory_write("ingest_summary", {"chat_id": "group-1"})
        self.assertIsNone(result)
        guard.check.assert_not_awaited()

    async def test_verified_automatic_fact_passes_without_candidate_review(self):
        guard = SimpleNamespace(check=AsyncMock(return_value=None), pending=lambda chat: False)
        inbox = SimpleNamespace(capture=AsyncMock())
        with (patch.object(heart_memory_backend, "settings", return_value={
                "auto_candidates_enabled": True, "auto_write_verified": True}),
              patch.object(heart_memory_backend, "guardian", return_value=guard),
              patch.object(heart_memory_backend, "candidate_inbox", return_value=inbox)):
            result = await heart_memory_backend.before_memory_write("ingest_text", {
                "chat_id": "group-1", "source_type": "person_fact",
                "metadata": {"writeback_source": "memory_flow_service"},
            })
        self.assertIsNone(result)
        inbox.capture.assert_not_awaited()

    async def test_native_summary_threshold_and_failure_retry_preserve_group(self):
        service = memory_flow_service.ChatSummaryWritebackService()
        message = SimpleNamespace(message_id="group-message", session=SimpleNamespace(
            session_id="group-1", group_id="qq-group-1", user_id="user-1"))
        count = 35
        ingest = AsyncMock(side_effect=[SimpleNamespace(success=False, detail="测试失败"),
                                       SimpleNamespace(success=True, detail="测试成功")])
        with (patch.object(service, "_extract_message_timestamp", return_value=1000),
              patch.object(service, "_count_messages_until_trigger", side_effect=lambda **kwargs: count),
              patch.object(service, "_load_last_trigger_message_count", AsyncMock(return_value=0)),
              patch.object(service, "_message_threshold", return_value=36),
              patch.object(service, "_context_length", return_value=36),
              patch.object(memory_flow_service.memory_service, "ingest_summary", ingest)):
            await service._handle_message(message)
            ingest.assert_not_awaited()
            count = 36
            await service._handle_message(message)
            self.assertEqual(service._states["group-1"].last_trigger_message_count, 0)
            await service._handle_message(message)
        self.assertEqual(service._states["group-1"].last_trigger_message_count, 36)
        self.assertEqual(ingest.await_count, 2)
        self.assertEqual(ingest.await_args.kwargs["chat_id"], "group-1")
        self.assertEqual(ingest.await_args.kwargs["group_id"], "qq-group-1")


if __name__ == "__main__":
    unittest.main()
