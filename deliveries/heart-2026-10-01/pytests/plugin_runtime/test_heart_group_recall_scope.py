"""群回忆只浏览当前群的真实长期记忆，并按写入时间取最近结果。"""

import json
import sqlite3
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from src.services import heart_memory_scope


class _Store:
    def __init__(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.db.execute(
            "CREATE TABLE paragraphs (content TEXT, source TEXT, metadata TEXT, "
            "created_at REAL, is_deleted INTEGER, expires_at REAL)"
        )

    def add(self, content, source, chat_id, created_at, *, person_id="", chat_ids=None):
        metadata = {"chat_id": chat_id}
        if chat_ids is not None:
            metadata["chat_ids"] = chat_ids
        if person_id:
            metadata["person_id"] = person_id
            metadata["person_ids"] = [person_id]
        self.db.execute(
            "INSERT INTO paragraphs VALUES (?, ?, ?, ?, 0, NULL)",
            (content, source, json.dumps(metadata), created_at),
        )

    def get_paragraphs_by_source(self, source):
        return [dict(row) for row in self.db.execute(
            "SELECT * FROM paragraphs WHERE source = ?", (source,)
        )]

    def query(self, sql, params=None):
        return [dict(row) for row in self.db.execute(sql, params or ())]


class _Kernel:
    def __init__(self, store):
        self.metadata_store = store

    def _get_search_hit_service(self):
        return SimpleNamespace(_filter_user_visible_hits=lambda hits: hits)


class GroupRecallScopeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = _Store()
        kernel = _Kernel(self.store)

        async def kernel_for_read():
            return kernel

        self.patch_kernel = patch.object(heart_memory_scope, "kernel_for_read", kernel_for_read)
        self.patch_session = patch.object(
            heart_memory_scope, "session_for", lambda args: SimpleNamespace(group_id="group-1")
        )
        self.patch_kernel.start()
        self.patch_session.start()
        self.addCleanup(self.patch_kernel.stop)
        self.addCleanup(self.patch_session.stop)
        self.addCleanup(self.store.db.close)

    async def test_plain_recall_shows_latest_group_facts_and_summaries(self):
        for index in range(1, 7):
            self.store.add(f"本群摘要{index}", "chat_summary:group-1", "group-1", index)
        self.store.add("本群人物事实", "person_fact:person-1", "group-1", 7, person_id="person-1")
        self.store.add("私聊人物事实", "person_fact:person-1", "private-1", 100, person_id="person-1")
        self.store.add("其他群摘要", "chat_summary:group-2", "group-2", 101)
        self.store.add("混合来源事实", "person_fact:person-2", "group-1", 102,
                       person_id="person-2", chat_ids=["group-1", "private-1"])

        result = await heart_memory_scope.prepare_read("heart_scoped_list", {
            "chat_id": "group-1", "limit": 5, "include_group_summaries": True,
        })

        self.assertTrue(result["success"])
        self.assertEqual([hit["content"] for hit in result["hits"]], [
            "本群人物事实", "本群摘要6", "本群摘要5", "本群摘要4", "本群摘要3",
        ])

    async def test_targeted_recall_filters_before_limiting_summaries(self):
        self.store.add("较早的本群讨论提及小明", "chat_summary:group-1", "group-1", 1)
        for index in range(2, 12):
            self.store.add(f"其他话题摘要{index}", "chat_summary:group-1", "group-1", index)

        result = await heart_memory_scope.prepare_read("heart_scoped_list", {
            "chat_id": "group-1", "person_id": "person-1", "target_name": "小明",
            "query": "小明", "limit": 5, "include_group_summaries": True,
        })

        self.assertEqual([hit["content"] for hit in result["hits"]], ["较早的本群讨论提及小明"])
        self.assertTrue(result["hits"][0]["group_context_only"])

    async def test_group_search_forces_current_chat_scope(self):
        kernel = await heart_memory_scope.kernel_for_read()
        requests = []

        async def search_memory(request):
            requests.append(request)
            return {"success": True, "hits": []}

        kernel.search_memory = search_memory
        result = await heart_memory_scope.prepare_read("search_memory", {
            "chat_id": "group-1", "query": "过去聊过什么", "limit": 5,
            "shared_chat_ids": ["private-1"], "respect_filter": False,
        })

        self.assertTrue(result["success"])
        self.assertEqual(requests[0].chat_id, "group-1")
        self.assertEqual(requests[0].shared_chat_ids, ())
        self.assertTrue(requests[0].respect_filter)


class MoodTimeoutLogTests(unittest.IsolatedAsyncioTestCase):
    async def test_timeout_warning_renders_plugin_name(self):
        from src.plugin_runtime.capabilities import core as capability_core
        from src.services import heart_llm_budget, llm_service

        async def time_out(*_args):
            raise TimeoutError

        warnings = []
        with (patch.object(llm_service, "get_available_models", return_value={"utils": object()}),
              patch.object(llm_service, "resolve_task_name", side_effect=lambda name: name),
              patch.object(heart_llm_budget, "generate_with_budget", time_out),
              patch.object(capability_core, "logger", SimpleNamespace(warning=warnings.append))):
            result = await capability_core.RuntimeCoreCapabilityMixin()._cap_llm_generate(
                "heart.mood", "llm.generate", {
                    "prompt": "评分", "task_name": "utils", "request_timeout_seconds": 20,
                }
            )

        self.assertEqual(result["error_code"], "request_timeout")
        self.assertEqual(warnings, ["插件模型请求达到调用方时限，已执行本地取消清理：heart.mood"])


if __name__ == "__main__":
    unittest.main()
