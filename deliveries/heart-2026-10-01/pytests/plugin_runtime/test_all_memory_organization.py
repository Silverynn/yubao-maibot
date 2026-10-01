"""全量整理的隔离测试：不连接真实记忆库或模型。"""
from types import SimpleNamespace
from unittest.mock import AsyncMock
import json
import time
import unittest

from scripts.organize_all_memories import validate_target
from src.A_memorix.core.utils.episode_service import EpisodeService
from src.A_memorix.core.utils.hash import compute_paragraph_hash


class OrganizationTests(unittest.IsolatedAsyncioTestCase):
    def test_merge_cannot_cross_session_or_remove_keeper(self):
        row = {'source':'chat_summary:group1','content':'小明喜欢围棋','hash':compute_paragraph_hash('小明喜欢围棋')}
        validate_target(row)
        with self.assertRaises(ValueError):
            validate_target(row, dict(row, source='chat_summary:private1'))
        with self.assertRaises(ValueError):
            validate_target(row, row)
        with self.assertRaises(ValueError):
            validate_target(dict(row, content='已经变化的正文'))

    async def test_rebuild_reads_only_effective_evidence(self):
        now = time.time()
        rows = [dict(hash='active',content='有效事实',metadata={},created_at=now),
                dict(hash='expired',content='过期事实',metadata={},expires_at=now-1),
                dict(hash='superseded',content='已替换',metadata=json.dumps({'memory_change':{'change_type':'mark_superseded'}})),
                dict(hash='ended',content='失效事实',metadata={'memory_change':{'valid_to':now-1}}),
                dict(hash='future',content='仍有效',metadata={'memory_change':{'valid_to':now+3600}},created_at=now)]
        db = SimpleNamespace(get_live_paragraphs_by_source=lambda *args,**kwargs:rows,
            get_paragraph_entities_by_hashes=lambda hashes:{},get_episodes_by_source=lambda source:[])
        service = EpisodeService(metadata_store=db,segmentation_service=SimpleNamespace(generation_signature=lambda:{}))
        async def build(group):
            return {'payloads':[{'evidence_ids':[p['hash'] for p in group['paragraphs']]}]}
        service._build_episode_payloads_for_group=AsyncMock(side_effect=build)
        plan = await service.plan_source_rebuild('chat_summary:group1')
        self.assertEqual(plan['paragraph_count'],2)
        self.assertEqual({key for item in plan['payloads'] for key in item['evidence_ids']},{'active','future'})

    async def test_all_invalid_source_clears_old_episodes_without_model(self):
        db = SimpleNamespace(get_live_paragraphs_by_source=lambda *args,**kwargs:[{'metadata':{'memory_change':{'valid_to':1}}}],
                             replace_episodes_for_source=lambda source,payloads:{'episode_count':len(payloads)})
        segment = SimpleNamespace(generation_signature=lambda:{},segment=AsyncMock())
        service = EpisodeService(metadata_store=db,segmentation_service=segment)
        result = await service.rebuild_source('chat_summary:group1')
        self.assertEqual(result['episode_count'],0)
        segment.segment.assert_not_awaited()
