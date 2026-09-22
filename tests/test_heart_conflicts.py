"""虚构数据验证先确认、本人确认、失败不误报；不连接模型或QQ。"""
import asyncio
import copy
import sys
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "extensions"))
from heart_shared.conflicts import ConflictGuard, own_hits, validate_plan
from heart_shared.storage import AuditStore


class Backend:
    def __init__(self):
        self.hit = {"hash": "old", "content": "测试同学想学Python", "type": "paragraph",
                    "metadata": {"person_ids": ["owner"], "source_type": "person_fact"}}
        self.judgment = {"supported": True, "confidence": .95, "verdict": "conflict", "conflict_ids": ["old"]}
        self.actor = {"person_id": "owner", "request_id": 1, "cancel": False}
        self.sent, self.calls = [], []
        self.prepared = {"stored_ids": ["new"]}
        self.result = {"success": True, "execution": {"superseded_targets": [{"hash": "old"}]}}

    async def owner(self, args):
        return {"person_id": "owner", "name": "测试同学", "evidence": ["我现在不想学Python"]}

    async def search(self, args, limit):
        return {"hits": [self.hit]}

    async def judge(self, *args):
        return self.judgment

    async def preview(self, proposal):
        self.record = {"status": "awaiting_confirmation", "plan": {
            "person_id": "owner", "chat_id": "chat", "operations": [
                {"action": "mark_superseded", "target_type": "paragraph", "hash": "old"},
                {"action": "ingest_text", "source_type": "person_fact", "chat_id": "chat",
                 "person_ids": ["owner"], "text": proposal["args"]["text"]},
                {"action": "refresh_person_profile", "person_id": "owner"}]}}
        return {"success": True, "plan_id": "plan1", "plan": self.record}

    async def private_session(self, owner):
        return "private"

    async def send(self, session, text):
        self.sent.append((session, text))
        return True

    async def confirmation_actor(self, session):
        return self.actor

    async def get_plan(self, plan_id):
        return self.record

    async def prepare_new(self, *args):
        self.calls.append("prepare")
        return self.prepared

    async def execute_plan(self, plan_id):
        self.calls.append("execute")
        return self.result


class ConflictTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = AuditStore(ROOT / ".runtime/test-data" / uuid.uuid4().hex)
        self.backend = Backend()
        self.now = 1000
        self.guard = ConflictGuard(self.store, self.backend, lambda: self.now)
        self.args = {"text": "测试同学不想学Python", "chat_id": "chat", "person_ids": ["owner"]}
        self.config = {"candidate_limit": 15, "timeout_seconds": 1, "confirmation_minutes": 10}

    async def propose(self):
        result = await self.guard.check(self.args, self.config)
        self.assertFalse(result["success"])
        self.assertEqual(self.backend.calls, [])

    async def finish(self):
        await self.guard.resolve("private")
        if self.guard.tasks:
            await asyncio.gather(*self.guard.tasks)

    def status(self):
        with self.store.connect() as db:
            return db.execute("SELECT status FROM heart_proposals WHERE id=1").fetchone()[0]

    async def test_no_write_before_confirmation_and_only_once(self):
        await self.propose()
        self.assertEqual(self.status(), "pending")
        self.assertEqual(self.backend.sent[0][0], "private")
        await self.finish()
        self.assertEqual(self.backend.calls, ["prepare", "execute"])
        self.assertEqual(self.status(), "applied")
        await self.finish()
        self.assertEqual(len(self.backend.calls), 2)

    async def test_cancel(self):
        await self.propose()
        self.backend.actor["cancel"] = True
        await self.finish()
        self.assertEqual(self.status(), "cancelled")
        self.assertEqual(self.backend.calls, [])

    async def test_expired(self):
        await self.propose()
        self.now += 601
        await self.finish()
        self.assertEqual(self.status(), "expired")
        self.assertEqual(self.backend.calls, [])

    async def test_wrong_user_and_wrong_session_cannot_confirm(self):
        await self.propose()
        self.backend.actor["person_id"] = "stranger"
        await self.finish()
        self.backend.actor["person_id"] = "owner"
        await self.guard.resolve("group")
        self.assertEqual(self.status(), "pending")
        self.assertEqual(self.backend.calls, [])

    async def test_duplicate_and_restart(self):
        await self.propose()
        self.guard = ConflictGuard(self.store, self.backend, lambda: self.now)
        await self.propose()
        self.assertEqual(len(self.backend.sent), 1)
        await self.finish()
        self.assertEqual(self.status(), "applied")

    async def test_prepare_failure_never_invalidates_old(self):
        await self.propose()
        self.backend.prepared = {"stored_ids": [], "success": False}
        await self.finish()
        self.assertEqual(self.backend.calls, ["prepare"])
        self.assertEqual(self.status(), "failed")

    async def test_partial_failure_not_reported_as_success(self):
        await self.propose()
        self.backend.result = {"success": False}
        await self.finish()
        self.assertEqual(self.status(), "failed")
        self.assertIn("未全部完成", self.backend.sent[-1][1])

    async def test_stale_plan_and_old_memory(self):
        await self.propose()
        self.backend.record["plan"]["person_id"] = "other"
        await self.finish()
        self.assertEqual(self.backend.calls, [])

    async def test_changed_old_memory(self):
        await self.propose()
        self.backend.hit = copy.deepcopy(self.backend.hit)
        self.backend.hit["content"] = "已经变更的内容"
        await self.finish()
        self.assertEqual(self.backend.calls, [])

    async def test_uncertain_and_invalid_ids_do_not_pass(self):
        for patch in ({"verdict": "uncertain"}, {"confidence": float("nan")}, {"conflict_ids": ["other"]}):
            original = dict(self.backend.judgment)
            self.backend.judgment.update(patch)
            result = await self.guard.check(self.args, self.config)
            self.assertFalse(result["success"])
            self.backend.judgment = original
        self.assertEqual(self.backend.sent, [])

    async def test_clear_can_proceed(self):
        self.backend.judgment.update(verdict="clear", conflict_ids=[])
        self.assertIsNone(await self.guard.check(self.args, self.config))

    async def test_model_timeout_does_not_pass(self):
        async def timeout(*args):
            raise TimeoutError()
        self.backend.judge = timeout
        self.assertFalse((await self.guard.check(self.args, self.config))["success"])

    async def test_plan_scope_checks(self):
        proposal = {"args": self.args, "owner": {"person_id": "owner", "name": "测试同学"}, "old": [self.backend.hit]}
        preview = await self.backend.preview(proposal)
        record = preview["plan"]
        validate_plan(record, proposal)
        record["plan"]["operations"][1]["relations"] = [{"target": "other"}]
        with self.assertRaises(ValueError):
            validate_plan(record, proposal)

    def test_other_people_and_retired_hits_excluded(self):
        other = copy.deepcopy(self.backend.hit)
        other["metadata"]["person_ids"] = ["other"]
        retired = copy.deepcopy(self.backend.hit)
        retired["metadata"]["memory_change"] = {"change_type": "mark_superseded"}
        self.assertEqual(own_hits({"hits": [other, retired]}, "owner"), [])


if __name__ == "__main__":
    unittest.main()
