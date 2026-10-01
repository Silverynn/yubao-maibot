"""管理页显示契约回归：隔离 SQLite、真实插件加载器，不启动 QQ 或调用模型。"""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import json
import shutil
import unittest
import uuid

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from src.A_memorix.core.storage.metadata_store import MetadataStore
from src.common.version import read_project_version
from src.plugin_runtime.runner.plugin_loader import PluginLoader
from src.webui.routers import memory
from src.webui.routers.plugin import management

ROOT = Path(__file__).resolve().parents[2]


def isolated_directory():
    # Windows 沙箱中 mkdtemp 的 0700 权限会让测试进程自己也无法读取；用正常继承权限。
    root = ROOT / ".runtime/test-data" / ("webui-display-" + uuid.uuid4().hex)
    root.mkdir(parents=True)
    return root


class SourceCountsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = isolated_directory()
        self.store = MetadataStore(self.temp / "native")
        self.store.connect()
        self.addCleanup(self.store.close)
        self.kernel_patch = patch.object(memory, "get_runtime_kernel", return_value=SimpleNamespace(metadata_store=self.store))
        self.kernel_patch.start()
        self.addCleanup(self.kernel_patch.stop)

    async def source_list(self):
        items = self.store.get_all_sources()
        with patch.object(memory.memory_service, "source_admin", AsyncMock(return_value={
            "success": True, "items": [{**row, "episode_rebuild_blocked": True} for row in items], "count": len(items),
        })):
            return await memory._source_list()

    async def test_counts_unique_relations_and_excludes_deleted_paragraphs(self):
        first = self.store.add_paragraph("甲的第一条", source="chat_summary:甲")
        second = self.store.add_paragraph("甲的第二条", source="chat_summary:甲")
        other = self.store.add_paragraph("乙的第一条", source="chat_summary:乙")
        deleted = self.store.add_paragraph("甲的已删除条目", source="chat_summary:甲")
        relation = self.store.add_relation("甲", "喜欢", "Python", source_paragraph=first)
        self.store.link_paragraph_relation(second, relation)
        self.store.add_relation("甲", "使用", "C++", source_paragraph=second)
        self.store.add_relation("乙", "喜欢", "画画", source_paragraph=other)
        self.store.add_relation("甲", "曾经使用", "过期工具", source_paragraph=deleted)
        self.store.query("UPDATE paragraphs SET is_deleted = 1 WHERE hash = ?", (deleted,))
        before = self.store.get_statistics()
        result = await self.source_list()
        by_source = {row["source"]: row for row in result["items"]}
        self.assertEqual(by_source["chat_summary:甲"]["paragraph_count"], 2)
        self.assertEqual(by_source["chat_summary:甲"]["relation_count"], 2)
        self.assertEqual(by_source["chat_summary:乙"]["paragraph_count"], 1)
        self.assertEqual(by_source["chat_summary:乙"]["relation_count"], 1)
        self.assertTrue(by_source["chat_summary:甲"]["episode_rebuild_blocked"])
        self.assertEqual(self.store.get_statistics(), before)

    async def test_paragraph_without_relation_reports_real_zero(self):
        self.store.add_paragraph("没有关系的事实", source="memory:无关系")
        item = (await self.source_list())["items"][0]
        self.assertEqual(item["paragraph_count"], 1)
        self.assertEqual(item["relation_count"], 0)

    async def test_empty_and_native_failure_are_preserved(self):
        for result in ({"success": True, "items": [], "count": 0}, {"success": False, "error": "测试故障"}):
            with patch.object(memory.memory_service, "source_admin", AsyncMock(return_value=result)):
                self.assertEqual(await memory._source_list(), result)

    async def test_unavailable_statistics_are_not_silently_reported_as_zero(self):
        self.store.add_paragraph("事实", source="memory:测试")
        with patch.object(memory, "_query_memory_records", side_effect=HTTPException(503, "未就绪")):
            with self.assertRaises(HTTPException):
                await self.source_list()


class PluginStatusTests(unittest.TestCase):
    def setUp(self):
        self.temp = isolated_directory()
        self.plugin = self.temp / "demo"
        self.plugin.mkdir()
        (self.plugin / "_manifest.json").write_text(json.dumps({
            "manifest_version": 2, "id": "test.demo", "name": "测试", "version": "1.0.0",
        }), encoding="utf-8")
        self.app = FastAPI()
        self.app.include_router(management.router, prefix="/plugins")
        self.client = TestClient(self.app)

    def query_status(self, *, running=False, loading=False, status=None, reason="", enabled=True):
        patches = (
            patch.object(management, "require_plugin_token", return_value="ok"),
            patch.object(management, "iter_plugin_directories", return_value=[self.plugin]),
            patch.object(management, "_get_runtime_plugin_load_statuses", return_value={} if status is None else {"test.demo": status}),
            patch.object(management, "_get_runtime_plugin_load_failure_reasons", return_value={"test.demo": reason}),
            patch.object(management, "_get_runtime_plugin_circuit_statuses", return_value={}),
            patch.object(management, "_is_runtime_loading", return_value=loading),
            patch.object(management, "_is_runtime_running", return_value=running),
            patch.object(management, "_read_plugin_enabled", return_value=enabled),
        )
        for p in patches:
            p.start()
        try:
            response = self.client.get("/plugins/installed")
            self.assertEqual(response.status_code, 200)
            return response.json()["plugins"][0]
        finally:
            for p in reversed(patches):
                p.stop()

    def test_cold_runtime_is_not_failed(self):
        item = self.query_status()
        self.assertEqual(item["load_status"], "not_started")
        self.assertFalse(item["loaded"])
        self.assertEqual(item["load_error"], "")

    def test_running_unknown_and_loading_are_distinct(self):
        self.assertEqual(self.query_status(running=True)["load_status"], "unknown")
        self.assertEqual(self.query_status(running=None)["load_status"], "unknown")
        self.assertEqual(self.query_status(loading=True)["load_status"], "loading")

    def test_real_failure_is_never_hidden_by_stopped_runtime(self):
        item = self.query_status(status="failed", reason="明确加载异常")
        self.assertEqual(item["load_status"], "failed")
        self.assertEqual(item["load_error"], "明确加载异常")
        self.assertEqual(self.query_status(reason="导入错误")["load_status"], "failed")

    def test_disabled_success_offline_preserved(self):
        self.assertEqual(self.query_status(enabled=False)["load_status"], "disabled")
        self.assertTrue(self.query_status(status="success")["loaded"])
        self.assertEqual(self.query_status(status="offline")["load_status"], "offline")

    def test_reads_public_runtime_property(self):
        from src.plugin_runtime import integration
        with patch.object(integration, "get_plugin_runtime_manager", return_value=SimpleNamespace(is_running=False)):
            self.assertIs(management._is_runtime_running(), False)
        with patch.object(integration, "get_plugin_runtime_manager", side_effect=RuntimeError("测试错误")):
            self.assertIsNone(management._is_runtime_running())


class InstalledPluginImportTests(unittest.TestCase):
    def test_installed_heart_plugins_pass_real_loader_without_starting_services(self):
        isolated = isolated_directory()
        for name in ("heart_memory_audit", "heart_mood"):
            shutil.copytree(ROOT / "plugins" / name, isolated / name, ignore=shutil.ignore_patterns("__pycache__", ".git"))
        loader = PluginLoader(host_version=read_project_version(ROOT))
        loaded = loader.discover_and_load([str(isolated)])
        self.assertEqual(loader.failed_plugins, {})
        self.assertEqual({meta.plugin_id for meta in loaded}, {"heart.memory-audit", "heart.mood"})


if __name__ == "__main__":
    unittest.main()
