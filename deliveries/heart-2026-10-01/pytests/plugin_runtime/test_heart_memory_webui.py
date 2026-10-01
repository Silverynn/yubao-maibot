"""隔离的原生 SQLite 和原生修正执行器；不启动 QQ、不调用真实模型。"""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import asyncio
import json
import unittest
import uuid

from heart_shared.candidates import CandidateInbox
from heart_shared.conflicts import ConflictGuard
from heart_shared.memory_scope import visible
from heart_shared.storage import AuditStore
from src.A_memorix.core.runtime.services.correction_admin_service import MemoryCorrectionAdminService
from src.A_memorix.core.storage.metadata_store import MetadataStore
from src.services import heart_memory_admin as admin, heart_memory_backend as backend, heart_memory_scope as scope


class WebUIGroupEditTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[2] / ".runtime/test-data" / uuid.uuid4().hex
        self.audit = AuditStore(root / "audit")
        self.metadata = MetadataStore(root / "native")
        self.metadata.connect()
        self.addCleanup(self.metadata.close)
        self.old = self.metadata.add_paragraph("测试群原摘要", source="chat_summary:group-1", metadata={"chat_id": "group-1"})
        self.private = self.metadata.add_paragraph("私聊秘密", source="person_fact:p1", metadata={"chat_id": "private-1"})
        self.kernel = SimpleNamespace(metadata_store=self.metadata, initialize=AsyncMock(),
            _current_effective_filter_cache={}, _persist=lambda: None, _rebuild_graph_from_metadata=lambda: None,
            _execute_fuzzy_modify_paragraph_cascade=lambda **kwargs: {},
            refresh_person_profile=AsyncMock(return_value={"success": True}),
            _get_search_hit_service=lambda: SimpleNamespace(_filter_user_visible_hits=lambda hits: hits))

        async def ingest(**args):
            result_hash = self.metadata.add_paragraph(args["text"], source=args["source_type"] + ":" + args["chat_id"],
                metadata={"chat_id": args["chat_id"], **args.get("metadata", {})})
            return {"success": True, "stored_ids": [result_hash]}

        self.kernel.ingest_text = ingest
        self.corrector = MemoryCorrectionAdminService(self.kernel)

        async def invoke(component, args):
            if component == "ingest_text":
                return await ingest(**args)
            self.assertEqual(component, "memory_correction_admin")
            return await self.corrector.memory_correction_admin(**args)

        for p in (
            patch.object(scope, "session_for", return_value=SimpleNamespace(session_id="group-1", group_id="qq-group-1")),
            patch.object(scope, "kernel_for_read", AsyncMock(return_value=self.kernel)),
            patch.object(backend, "settings", return_value={"plugin_enabled": True}),
            patch.object(backend, "AuditStore", return_value=self.audit),
            patch.object(backend.NativeBackend, "invoke", side_effect=invoke),
        ):
            p.start(); self.addCleanup(p.stop)

    async def preview(self):
        return await admin.preview_group_edit("group-1", self.old, "测试群原摘要", "管理员修正后的摘要", "纠正测试内容")

    async def test_preview_does_not_write_execute_updates_native_recall_and_audit(self):
        preview = await self.preview()
        self.assertEqual(self.metadata.get_paragraph(self.old)["content"], "测试群原摘要")
        self.assertEqual(len(self.metadata.get_paragraphs_by_source("chat_summary:group-1")), 1)
        result = await admin.execute_group_edit(preview["plan_id"])
        self.assertTrue(result["success"])
        self.assertFalse(visible(self.metadata.get_paragraph(self.old), "group-1"))
        listed = await admin.group_memories("group-1")
        self.assertEqual([item["text"] for item in listed["items"]], ["管理员修正后的摘要"])
        with self.assertRaises(ValueError):
            await admin.execute_group_edit(preview["plan_id"])
        text = "\n".join(p.read_text(encoding="utf-8-sig") for p in (self.audit.root / "logs").rglob("*.txt"))
        self.assertIn("原生记忆已修正", text)
        self.assertIn("不等于用户本人确认", text)

    async def test_cross_chat_and_stale_text_rejected(self):
        with self.assertRaises(ValueError):
            await admin.preview_group_edit("group-1", self.private, "私聊秘密", "泄露秘密", "测试")
        with self.assertRaises(ValueError):
            await admin.preview_group_edit("group-1", self.old, "错误旧内容", "新内容", "测试")

    async def test_changed_record_and_tampered_plan_rejected(self):
        preview = await self.preview()
        self.metadata.update_paragraph_metadata(self.old, {"new_change": "another editor"}, merge=True)
        with self.assertRaises(ValueError):
            await admin.execute_group_edit(preview["plan_id"])
        preview = await self.preview()
        record = self.metadata.get_fuzzy_modify_plan(preview["plan_id"])
        record["plan"]["operations"][1]["text"] = "被篡改的新内容"
        self.metadata.update_fuzzy_modify_plan(preview["plan_id"], plan=record["plan"])
        with self.assertRaises(ValueError):
            await admin.execute_group_edit(preview["plan_id"])

    async def test_new_write_failure_does_not_supersede_old(self):
        preview = await self.preview()
        with patch.object(backend.NativeBackend, "invoke", AsyncMock(return_value={"success": False, "stored_ids": []})):
            # get_plan 也经过 invoke，所以只替换写入出口。
            with patch.object(backend.NativeBackend, "get_plan", AsyncMock(return_value=self.metadata.get_fuzzy_modify_plan(preview["plan_id"]))):
                with self.assertRaises(ValueError):
                    await admin.execute_group_edit(preview["plan_id"])
        self.assertTrue(visible(self.metadata.get_paragraph(self.old), "group-1"))

    async def test_existing_body_cannot_overwrite_another_memory(self):
        with self.assertRaises(ValueError):
            await admin.preview_group_edit("group-1", self.old, "测试群原摘要", "私聊秘密", "测试")
        preview = await self.preview()
        self.metadata.add_paragraph("管理员修正后的摘要", source="chat_summary:group-2", metadata={"chat_id": "group-2"})
        with self.assertRaises(ValueError):
            await admin.execute_group_edit(preview["plan_id"])
        self.assertTrue(visible(self.metadata.get_paragraph(self.old), "group-1"))


class AdminCandidateTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[2] / ".runtime/test-data" / uuid.uuid4().hex
        self.audit = AuditStore(root)
        self.native = SimpleNamespace(owner=AsyncMock(return_value={"person_id": "p1", "name": "小明", "evidence": ["我喜欢围棋"]}),
            write_reviewed_candidate=AsyncMock(return_value={"success": True, "stored_ids": ["new"]}),
            conflict_pending=lambda chat, result: bool(result.get("pending")))
        self.inbox = CandidateInbox(self.audit, self.native)
        self.args = {"chat_id": "group-1", "text": "小明喜欢围棋", "person_ids": ["p1"], "metadata": {"evidence_message_ids": ["source"]}}

    async def candidate(self):
        await self.inbox.capture(self.args, reason="模型判断不确定，等待管理员审核")
        return self.inbox.list_for("p1")[0]["id"]

    async def test_admin_edit_approval_preserves_scope_and_marks_admin_not_user(self):
        cid = await self.candidate()
        await self.inbox.review(cid, True, self.args["text"], "小明喜欢研究围棋")
        args = self.native.write_reviewed_candidate.await_args.args[0]
        self.assertEqual(args["chat_id"], "group-1")
        self.assertEqual(args["person_ids"], ["p1"])
        self.assertEqual(args["metadata"]["heart_review"]["confirmed_by"], "administrator")
        self.assertEqual(args["metadata"]["heart_review"]["original_text"], self.args["text"])
        self.assertEqual(self.inbox.list_for("p1"), [])

    async def test_reject_and_stale_approval_do_not_write(self):
        cid = await self.candidate()
        with self.assertRaises(ValueError):
            await self.inbox.review(cid, True, "过时内容")
        await self.inbox.review(cid, False, self.args["text"])
        self.native.write_reviewed_candidate.assert_not_awaited()

    async def test_filter_and_delete_review_record_preserves_native_memory(self):
        from src.services.heart_memory_admin import candidate_list
        cid = await self.candidate()
        with patch("src.services.heart_memory_backend.candidate_inbox", return_value=self.inbox):
            self.assertEqual(len(candidate_list(status="pending")["items"]), 1)
            self.assertEqual(candidate_list(status="written")["items"], [])
        with self.audit.connect() as db:
            db.execute("UPDATE heart_candidates SET status='conflict_pending' WHERE id=?", (cid,))
            db.commit()
        with self.assertRaises(ValueError):
            await self.inbox.delete_review_record(cid, self.args["text"])
        with self.audit.connect() as db:
            db.execute("UPDATE heart_candidates SET status='written' WHERE id=?", (cid,))
            db.commit()
        with self.assertRaises(ValueError):
            await self.inbox.delete_review_record(cid, "过期的内容")
        result = await self.inbox.delete_review_record(cid, self.args["text"])
        self.assertTrue(result["success"])
        with self.audit.connect() as db:
            self.assertIsNone(db.execute("SELECT id FROM heart_candidates WHERE id=?", (cid,)).fetchone())
        self.native.write_reviewed_candidate.assert_not_awaited()

    async def test_real_native_payload_without_success_is_normalized(self):
        from src.services.heart_memory_backend import NativeBackend
        native = NativeBackend()
        for raw, expected in [({'stored_ids':['actual-hash']}, True),
                              ({'skipped_ids':['existing-hash']}, True),
                              ({'success':False,'stored_ids':['partial-hash']}, False),
                              ({'error':'失败','stored_ids':['partial-hash']}, False),
                              ({'detail':'queued'}, False)]:
            with patch.object(native, 'invoke', AsyncMock(return_value=raw)):
                result = await native.write_reviewed_candidate(self.args)
                self.assertEqual(result.get('success'), expected)
        cid = await self.candidate()
        with patch.object(native, 'invoke', AsyncMock(return_value={'stored_ids':['actual-hash']})):
            self.inbox.backend = native
            await self.inbox.review(cid, True, self.args['text'])
        with self.audit.connect() as db:
            self.assertEqual(db.execute('SELECT status FROM heart_candidates WHERE id=?',(cid,)).fetchone()[0], 'written')

    async def test_admin_approval_keeps_conflict_pending(self):
        cid = await self.candidate()
        self.native.write_reviewed_candidate.return_value = {"success": False, "pending": True}
        await self.inbox.review(cid, True, self.args["text"])
        with self.audit.connect() as db:
            status = db.execute("SELECT status FROM heart_candidates WHERE id=?", (cid,)).fetchone()[0]
        self.assertEqual(status, "conflict_pending")


class GroupConfirmationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[2] / ".runtime/test-data" / uuid.uuid4().hex
        self.audit = AuditStore(root)
        old = {"hash": "old", "content": "小明喜欢围棋", "type": "paragraph",
               "metadata": {"person_ids": ["p1"], "chat_id": "group-1", "source_type": "person_fact"}}
        self.actor = {"person_id": "p1", "request_id": 1, "cancel": False}
        self.record = {"status": "awaiting_confirmation", "plan": {"person_id": "p1", "chat_id": "group-1", "operations": [
            {"action": "mark_superseded", "target_type": "paragraph", "hash": "old"},
            {"action": "ingest_text", "source_type": "person_fact", "text": "小明不喜欢围棋", "chat_id": "group-1", "person_ids": ["p1"]},
            {"action": "refresh_person_profile", "person_id": "p1"}]}}
        self.native = SimpleNamespace(
            owner=AsyncMock(return_value={"person_id": "p1", "name": "小明", "evidence": ["我不喜欢围棋"]}),
            search=AsyncMock(return_value={"hits": [old]}),
            judge=AsyncMock(return_value={"supported": True, "confidence": .95, "verdict": "conflict", "conflict_ids": ["old"]}),
            preview=AsyncMock(return_value={"success": True, "plan_id": "native-plan", "plan": self.record}),
            confirmation_destination=AsyncMock(return_value="group-1"), send=AsyncMock(return_value=True),
            confirmation_actor=AsyncMock(side_effect=lambda *args: self.actor),
            get_plan=AsyncMock(return_value=self.record), prepare_new=AsyncMock(return_value={"stored_ids": ["new"]}),
            execute_plan=AsyncMock(return_value={"success": True, "execution": {"superseded_targets": [{"hash": "old"}]}}))
        self.guard = ConflictGuard(self.audit, self.native)

    async def propose(self):
        return await self.guard.check({"chat_id": "group-1", "group_id": "qq-group-1", "text": "小明不喜欢围棋", "person_ids": ["p1"]},
                                     {"candidate_limit": 15, "timeout_seconds": 1, "confirmation_minutes": 10})

    async def test_group_ask_only_original_person_in_same_group_can_confirm(self):
        result = await self.propose()
        self.assertTrue(result["pending"])
        self.assertEqual(result["confirmation_mode"], "group")
        self.assertEqual(self.native.send.await_args.args[0], "group-1")
        self.actor["person_id"] = "someone-else"
        self.assertIn("没有找到属于你", await self.guard.resolve("group-1", "reply-1"))
        self.actor["person_id"] = "p1"
        self.assertIn("没有找到属于你", await self.guard.resolve("group-2", "reply-2"))
        self.native.prepare_new.assert_not_awaited()
        await self.guard.resolve("group-1", "reply-3")
        await asyncio.gather(*self.guard.tasks)
        self.native.prepare_new.assert_awaited_once()
        self.native.execute_plan.assert_awaited_once()

    async def test_group_destination_rejects_private_memory(self):
        native = backend.NativeBackend()
        from src.chat.message_receive.chat_manager import chat_manager
        with patch.object(chat_manager, "get_existing_session_by_session_id", return_value=SimpleNamespace(group_id="qq-group-1", session_id="group-1")):
            with self.assertRaises(ValueError):
                await native.confirmation_destination({"person_id": "p1"}, {"chat_id": "group-1"}, [{
                    "content": "私人秘密", "metadata": {"chat_id": "private-1", "person_ids": ["p1"]}}])

    async def test_confirmation_message_identity_does_not_use_latest_other_user(self):
        native = backend.NativeBackend()
        from src.chat.message_receive.chat_manager import chat_manager
        from src.common import message_repository
        from src.chat.utils import utils
        from src.person_info import person_info
        original = SimpleNamespace(session_id="group-1", message_id="confirm-id", platform="qq",
            processed_plain_text="/确认记忆更新 1", message_info=SimpleNamespace(user_info=SimpleNamespace(user_id="original-user")))
        with (patch.object(message_repository, "find_messages", return_value=[original]),
              patch.object(utils, "is_bot_self", return_value=False),
              patch.object(person_info, "get_person_id", side_effect=lambda platform, user: user),
              patch.object(chat_manager, "last_messages", {"group-1": SimpleNamespace(message_id="another-message")})):
            actor = await native.confirmation_actor("group-1", "confirm-id")
        self.assertEqual(actor["person_id"], "original-user")

    async def test_send_failure_does_not_report_pending_confirmation(self):
        self.native.send.return_value = False
        result = await self.propose()
        self.assertFalse(result.get("pending", False))
        self.assertIn("发送失败", result["detail"])
        self.native.prepare_new.assert_not_awaited()

    async def test_candidate_status_follows_group_conflict_completion(self):
        inbox = CandidateInbox(self.audit, self.native)
        args = {"chat_id": "group-1", "group_id": "qq-group-1", "text": "小明不喜欢围棋", "person_ids": ["p1"], "metadata": {}}
        await inbox.capture(args)
        async def reviewed_write(reviewed):
            return await self.guard.check(reviewed,
                {"candidate_limit": 15, "timeout_seconds": 1, "confirmation_minutes": 10})
        self.native.write_reviewed_candidate = AsyncMock(side_effect=reviewed_write)
        self.native.conflict_pending = lambda chat, result: bool(result.get("pending"))
        await inbox.review(1, True, args["text"])
        with self.audit.connect() as db:
            self.assertEqual(db.execute("SELECT status FROM heart_candidates WHERE id=1").fetchone()[0], "conflict_pending")
        await self.guard.resolve("group-1", "confirmation-id")
        await asyncio.gather(*self.guard.tasks)
        with self.audit.connect() as db:
            self.assertEqual(db.execute("SELECT status FROM heart_candidates WHERE id=1").fetchone()[0], "written")


class WebUIAuthorizationTests(unittest.IsolatedAsyncioTestCase):
    async def test_authenticated_api_review_uses_same_candidate_inbox(self):
        from fastapi import FastAPI
        from httpx import ASGITransport, AsyncClient
        from src.webui.dependencies import require_auth
        from src.webui.routers.memory import router
        root = Path(__file__).resolve().parents[2] / ".runtime/test-data" / uuid.uuid4().hex
        store = AuditStore(root)
        native = SimpleNamespace(owner=AsyncMock(return_value={"person_id": "p1", "name": "小明", "evidence": ["我喜欢围棋"]}),
            write_reviewed_candidate=AsyncMock(return_value={"success": True, "stored_ids": ["new"]}),
            conflict_pending=lambda chat, result: False)
        inbox = CandidateInbox(store, native)
        await inbox.capture({"chat_id": "group-1", "text": "小明喜欢围棋", "metadata": {}})
        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[require_auth] = lambda: "test-admin"
        with (patch.object(backend, "candidate_inbox", return_value=inbox),
              patch.object(backend, "settings", return_value={"plugin_enabled": True})):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                listed = await client.get("/memory/heart/candidates?chat_id=group-1")
                self.assertEqual(listed.json()["items"][0]["evidence"], ["我喜欢围棋"])
                result = await client.post("/memory/heart/candidates/1/review", json={
                    "approve": True, "expected_text": "小明喜欢围棋", "edited_text": "小明喜欢研究围棋"})
                self.assertEqual(result.json()["status"], "written")
        self.assertEqual(native.write_reviewed_candidate.await_args.args[0]["chat_id"], "group-1")

    async def test_local_console_marker_respects_explicit_environment_override(self):
        from src.webui import app as webui_app
        root = Path(__file__).resolve().parents[2] / ".runtime/test-data" / uuid.uuid4().hex
        marker = root / "dashboard/dist/heart-memory-console.json"
        marker.parent.mkdir(parents=True)
        marker.touch()
        with (patch.object(webui_app, "_get_project_root", return_value=root),
              patch.object(webui_app, "getenv", return_value="")):
            self.assertTrue(webui_app._is_local_dashboard_enabled())
        with (patch.object(webui_app, "_get_project_root", return_value=root),
              patch.object(webui_app, "getenv", return_value="0")):
            self.assertFalse(webui_app._is_local_dashboard_enabled())

    async def test_routes_require_authentication(self):
        from fastapi import FastAPI
        from httpx import ASGITransport, AsyncClient
        from src.webui.routers.memory import router
        app = FastAPI()
        app.include_router(router)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            responses = [await client.get("/memory/heart/candidates"),
                         await client.get("/memory/heart/group-memories?chat_id=group-1"),
                         await client.post("/memory/heart/candidates/1/review", json={"approve": True, "expected_text": "test"}),
                         await client.post("/memory/heart/group-edit/execute", json={"plan_id": "test", "confirmed": True})]
        self.assertTrue(all(response.status_code == 401 for response in responses))

    async def test_missing_explicit_confirmation_never_executes(self):
        from fastapi import FastAPI
        from httpx import ASGITransport, AsyncClient
        from src.webui.dependencies import require_auth
        from src.webui.routers.memory import router
        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[require_auth] = lambda: "test-admin"
        executor = AsyncMock()
        with patch.object(admin, "execute_group_edit", executor):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                response = await client.post("/memory/heart/group-edit/execute", json={"plan_id": "test", "confirmed": False})
        self.assertEqual(response.status_code, 400)
        executor.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
