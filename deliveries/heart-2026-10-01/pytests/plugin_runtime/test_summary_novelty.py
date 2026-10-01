import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import uuid

from heart_shared.summary_novelty import novelty, load_cursor, save_cursor
from heart_shared.storage import AuditStore
from src.A_memorix.core.utils.summary_importer import SummaryImporter, SUMMARY_PROMPT_TEMPLATE


class NoveltyTests(unittest.IsolatedAsyncioTestCase):
    def test_exact_and_format_duplicate(self):
        self.assertEqual(novelty('小明今天确认喜欢围棋。', [{'hash':'old', 'content':'小明今天确认喜欢围棋'}]), ('duplicate','old'))

    def test_changed_fact_and_new_detail_preserved(self):
        old = [{'hash':'old', 'content':'小明喜欢围棋'}]
        self.assertEqual(novelty('小明不喜欢围棋', old)[0], 'new')
        self.assertEqual(novelty('小明喜欢围棋，周五参加比赛', old)[0], 'new')

    def test_empty_and_prompt_format(self):
        self.assertEqual(novelty('', [])[0], 'empty')
        self.assertIn('"summary":""', SUMMARY_PROMPT_TEMPLATE.format(bot_name='测试', personality_context='', previous_summary_context='', chat_history='测试'))

    def test_superseded_record_cannot_block_new_write(self):
        self.assertEqual(novelty('小明今天确认喜欢围棋', [{'hash':'old','content':'小明今天确认喜欢围棋',
            'metadata':{'memory_change':{'valid_to':1}}}])[0], 'new')

    async def test_summary_service_successful_skip_needs_no_hash(self):
        from src.A_memorix.core.runtime.services.summary_service import MemorySummaryService
        from src.A_memorix.core.utils.summary_importer import SummaryImportResult
        kernel = SimpleNamespace(initialize=AsyncMock(), summary_importer=SimpleNamespace(import_from_stream=AsyncMock(
            return_value=SummaryImportResult(True,'无新增',source='chat_summary:test',skipped=True))), _persist=lambda: None)
        result = await MemorySummaryService.summarize_chat_stream(kernel,chat_id='test')
        self.assertTrue(result['success'])
        self.assertTrue(result['skipped'])
        self.assertEqual(result['stored_ids'], [])

    def test_cursor_survives_without_summary(self):
        store = AuditStore(Path(__file__).resolve().parents[2] / '.runtime/test-data' / uuid.uuid4().hex)
        with patch('heart_shared.storage.AuditStore', return_value=store):
            save_cursor('group-test', 100)
            save_cursor('group-test', 90)
            self.assertEqual(load_cursor('group-test'), 100)

    async def generate(self, content, prior=()):
        prior = [dict(row, source='chat_summary:test-group', metadata=row.get('metadata') or {'chat_id':'test-group'}) for row in prior]
        importer = SummaryImporter(vector_store=None, graph_store=None,
            metadata_store=SimpleNamespace(get_live_paragraphs_by_source=lambda source: list(prior)), embedding_manager=None, plugin_config={})
        importer._existing_summary_result = lambda **kwargs: None
        importer._ensure_runtime_self_check = AsyncMock(return_value=(True, 'ok'))
        importer._build_previous_summary_context = lambda *args, **kwargs: ''
        importer._resolve_summary_model_task = lambda: ('memory', SimpleNamespace(model_list=['mock']))
        importer._execute_import = AsyncMock(return_value='new-hash')
        importer._persist_import = lambda: None
        completion = SimpleNamespace(success=True, completion=SimpleNamespace(response=json.dumps(content)))
        with (patch('src.A_memorix.core.utils.summary_importer.message_api.get_messages_by_time_in_chat', return_value=[SimpleNamespace(timestamp=1)]),
              patch('src.A_memorix.core.utils.summary_importer.message_api.build_readable_messages', return_value='测试聊天'),
              patch('src.A_memorix.core.utils.summary_importer.llm_api.generate', AsyncMock(return_value=completion)),
              patch('heart_shared.summary_semantic.record_decision')):
            result = await importer._import_from_stream_unlocked('test-group', metadata={})
        return importer, result

    async def test_empty_success_never_writes(self):
        importer, result = await self.generate({'summary':'','entities':[],'relations':[]})
        self.assertTrue(result.success)
        importer._execute_import.assert_not_awaited()

    async def test_duplicate_never_writes(self):
        importer, result = await self.generate({'summary':'小明今天确认喜欢围棋','entities':[],'relations':[]}, [{'hash':'old','content':'小明今天确认喜欢围棋'}])
        self.assertTrue(result.success)
        importer._execute_import.assert_not_awaited()

    async def test_empty_inconsistent_result_fails(self):
        importer, result = await self.generate({'summary':'','entities':['小明'],'relations':[]})
        self.assertFalse(result.success)
        importer._execute_import.assert_not_awaited()

    async def test_new_detail_native_write(self):
        importer, result = await self.generate({'summary':'小明周五参加围棋比赛','entities':[],'relations':[]})
        self.assertTrue(result.success)
        importer._execute_import.assert_awaited_once()
