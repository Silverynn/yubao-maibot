"""群聊隐私边界与可配置批量评估的离线回归。数据均为虚构。"""
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'extensions'))

from heart_shared.memory_scope import visible, filter_result
import heart_memory_scope as scoped
import heart_memory_backend as backend


class VisibilityTests(unittest.TestCase):
    def test_group_never_receives_private_or_unknown_origin(self):
        person = 'person-1'
        group = {'content': '小明说他喜欢下棋', 'metadata': {'chat_id': 'group-1', 'person_ids': [person]}}
        private = {'content': '小明私下说他害怕考试', 'metadata': {'chat_id': 'private-1', 'person_ids': [person]}}
        mixed = {'content': '跨会话合并内容', 'metadata': {'chat_ids': ['group-1', 'private-1'], 'person_ids': [person]}}
        unknown = {'content': '无来源内容', 'metadata': {'person_ids': [person]}}
        global_item = {'content': '设为全局的私人事实', 'metadata': {'chat_id': 'group-1', 'scope_type': 'global'}}
        result = filter_result({'success': True, 'summary': private['content'],
                                'hits': [group, private, mixed, unknown, global_item]}, 'group-1', person)
        self.assertEqual(result['hits'], [group])
        self.assertEqual(result['summary'], group['content'])
        self.assertEqual(result['scope_removed_count'], 4)
        self.assertFalse(visible(group, 'group-2', person))
        self.assertFalse(visible(group, 'group-1', 'someone-else'))

    def test_group_summary_visible_but_not_attributed_to_person(self):
        summary = {'content': '小明和小红讨论了游戏', 'source': 'chat_summary:group-1',
                   'metadata': {'chat_id': 'group-1'}}
        self.assertTrue(visible(summary, 'group-1'))
        self.assertFalse(visible(summary, 'group-1', 'person-1'))


class ScopedNativeListTests(unittest.IsolatedAsyncioTestCase):
    async def test_group_search_starts_with_current_chat_scope(self):
        observed = []
        class Request:
            def __init__(self, **kwargs):
                self.__dict__.update(kwargs)
        async def search(request):
            observed.append(request)
            return {'success': True, 'hits': []}
        kernel = types.SimpleNamespace(search_memory=search)
        names = ('src', 'src.A_memorix', 'src.A_memorix.core', 'src.A_memorix.core.runtime',
                 'src.A_memorix.core.runtime.sdk_memory_kernel')
        modules = {name: types.ModuleType(name) for name in names}
        modules[names[-1]].KernelSearchRequest = Request
        with (patch.dict(sys.modules, modules),
              patch.object(scoped, 'session_for', return_value=types.SimpleNamespace(group_id='group-real')),
              patch.object(scoped, 'kernel_for_read', AsyncMock(return_value=kernel))):
            result = await scoped.prepare_read('search_memory', {'chat_id': 'group-1', 'group_id': '',
                'query': '围棋', 'respect_filter': False, 'shared_chat_ids': ['private-1']})
        self.assertTrue(result['success'])
        self.assertEqual(observed[0].chat_id, 'group-1')
        self.assertEqual(observed[0].group_id, 'group-real')
        self.assertEqual(observed[0].shared_chat_ids, ())
        self.assertTrue(observed[0].respect_filter)

    async def test_native_source_list_filters_private_and_deleted(self):
        rows = [
            {'hash': 'one', 'source': 'person_fact:p1', 'content': '小明喜欢围棋', 'metadata': {'chat_id': 'group-1', 'person_ids': ['p1']}, 'created_at': 3},
            {'hash': 'two', 'source': 'person_fact:p1', 'content': '小明私聊提到的事', 'metadata': {'chat_id': 'private-1', 'person_ids': ['p1']}, 'created_at': 2},
            {'hash': 'three', 'source': 'person_fact:p1', 'content': '已删旧事实', 'metadata': {'chat_id': 'group-1', 'person_ids': ['p1']}, 'created_at': 1, 'is_deleted': 1},
        ]
        store = types.SimpleNamespace(get_paragraphs_by_source=lambda source: rows if source == 'person_fact:p1' else [])
        service = types.SimpleNamespace(_filter_user_visible_hits=lambda items: items)
        kernel = types.SimpleNamespace(metadata_store=store, _get_search_hit_service=lambda: service)
        with patch.object(scoped, 'kernel_for_read', AsyncMock(return_value=kernel)):
            hits = await scoped.scoped_paragraphs('group-1', 'p1')
        self.assertEqual([hit['hash'] for hit in hits], ['one'])

    async def test_stored_log_reads_real_body_only(self):
        row = {'hash': 'id-1', 'content': '小明喜欢围棋', 'source': 'person_fact:p1',
               'metadata': {'chat_id': 'group-1', 'person_ids': ['p1']}}
        store = types.SimpleNamespace(get_paragraph=lambda key: row if key == 'id-1' else None)
        kernel = types.SimpleNamespace(metadata_store=store)
        with patch.object(scoped, 'kernel_for_read', AsyncMock(return_value=kernel)):
            saved = await scoped.stored_contents('ingest_text', {'chat_id': 'group-1', 'text': '待写入文本'},
                                                 {'success': True, 'stored_ids': ['id-1']})
        self.assertEqual(saved, ['小明喜欢围棋'])

    async def test_profile_query_uses_only_current_chat_facts(self):
        rows = [
            {'hash': 'group', 'source': 'person_fact:p1', 'content': '小明公开喜欢围棋',
             'metadata': {'chat_id': 'group-1', 'person_ids': ['p1']}},
            {'hash': 'private', 'source': 'person_fact:p1', 'content': '小明私下害怕考试',
             'metadata': {'chat_id': 'private-1', 'person_ids': ['p1']}},
        ]
        store = types.SimpleNamespace(get_paragraphs_by_source=lambda source: rows)
        service = types.SimpleNamespace(_filter_user_visible_hits=lambda items: items)
        kernel = types.SimpleNamespace(metadata_store=store, _get_search_hit_service=lambda: service)
        with patch.object(scoped, 'kernel_for_read', AsyncMock(return_value=kernel)), \
             patch.object(scoped, 'session_for', return_value=types.SimpleNamespace(session_id='group-1')):
            result = await scoped.scoped_profile({'chat_id': 'group-1', 'person_id': 'p1'})
        self.assertIn('公开喜欢围棋', result['profile_text'])
        self.assertNotIn('私下害怕考试', result['profile_text'])

    async def test_group_summary_switch_never_changes_person_scope(self):
        person = {'hash': 'one', 'source': 'person_fact:p1', 'content': '小明喜欢围棋',
                  'metadata': {'chat_id': 'group-1', 'person_ids': ['p1']}}
        summary = {'hash': 'two', 'source': 'chat_summary:group-1', 'content': '本群一起讨论围棋',
                   'metadata': {'chat_id': 'group-1'}}
        private = {'hash': 'three', 'source': 'person_fact:p1', 'content': '小明私聊提到的事',
                   'metadata': {'chat_id': 'private-1', 'person_ids': ['p1']}}
        rows = {'person_fact:p1': [person, private], 'chat_summary:group-1': [summary]}
        store = types.SimpleNamespace(get_paragraphs_by_source=lambda source: rows.get(source, []))
        service = types.SimpleNamespace(_filter_user_visible_hits=lambda items: items)
        kernel = types.SimpleNamespace(metadata_store=store, _get_search_hit_service=lambda: service)
        with patch.object(scoped, 'kernel_for_read', AsyncMock(return_value=kernel)), \
             patch.object(scoped, 'session_for', return_value=types.SimpleNamespace(group_id='group-real')):
            result = await scoped.prepare_read('heart_scoped_list', {'chat_id': 'group-1',
                'person_id': 'p1', 'target_name': '小明', 'include_group_summaries': False})
        self.assertEqual([hit['content'] for hit in result['hits']], ['小明喜欢围棋'])


class NativeWriteCoexistenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_verified_auto_write_reaches_native_and_uncertain_stays_candidate(self):
        args = {'chat_id': 'group-1', 'source_type': 'person_fact', 'text': '小明喜欢围棋',
                'metadata': {'writeback_source': 'memory_flow_service', 'evidence_message_ids': ['msg-1']}}
        cfg = {'enabled': True, 'auto_candidates_enabled': True, 'auto_write_verified': True,
               'max_pending_per_person': 20}
        guard = types.SimpleNamespace(check=AsyncMock(return_value=None), pending=lambda _: False)
        inbox = types.SimpleNamespace(capture=AsyncMock(return_value={'success': False, 'detail': '已进入候选记忆'}))
        with patch.object(backend, 'settings', return_value=cfg), patch.object(backend, 'guardian', return_value=guard), \
             patch.object(backend, 'candidate_inbox', return_value=inbox):
            self.assertIsNone(await backend.before_memory_write('ingest_text', args))
            inbox.capture.assert_not_awaited()
            guard.check.return_value = {'success': False, 'detail': '冲突判断不确定'}
            result = await backend.before_memory_write('ingest_text', args)
            self.assertTrue(result['pending'])
            inbox.capture.assert_awaited_once()

    async def test_pending_conflict_does_not_freeze_native_group_summary(self):
        cfg = {'enabled': True, 'auto_candidates_enabled': True, 'auto_write_verified': True,
               'max_pending_per_person': 20}
        guard = types.SimpleNamespace(pending=lambda _: True)
        with patch.object(backend, 'settings', return_value=cfg), patch.object(backend, 'guardian', return_value=guard):
            self.assertIsNone(await backend.before_memory_write('ingest_summary', {'chat_id': 'group-1'}))


if __name__ == '__main__':
    unittest.main()
