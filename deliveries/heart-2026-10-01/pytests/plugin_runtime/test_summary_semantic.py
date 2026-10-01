"""语义去重离线测试，不调用外部模型，不修改真实长期记忆。"""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import json
import unittest
import uuid

import numpy as np

from heart_shared import summary_semantic as semantic
from heart_shared.candidates import CandidateInbox
from heart_shared.readable import render_event
from heart_shared.storage import AuditStore
from heart_shared.summary_novelty import novelty
from src.A_memorix.core.utils.summary_importer import SummaryImporter


CONFIG = {'enabled':True,'candidate_limit':4,'timeout_seconds':1,'min_confidence':0.9,
          'max_tokens':4096,'guidance':'谨慎核对否定、数字和时间'}


def old(text='小明喜欢Python', key='old', chat='group1', **kwargs):
    return {'hash':key,'content':text,'source':'chat_summary:'+chat,'metadata':{'chat_id':chat},**kwargs}


def importer(rows):
    return SimpleNamespace(metadata_store=SimpleNamespace(get_live_paragraphs_by_source=lambda source:rows),
                           embedding_manager=None,vector_store=None,plugin_config={})


class SemanticTests(unittest.IsolatedAsyncioTestCase):
    def test_substring_negation_cannot_be_exact_duplicate(self):
        self.assertEqual(novelty('喜欢每天晚上花两个小时学习Python',
                                [old('不喜欢每天晚上花两个小时学习Python')])[0],'new')

    async def test_exact_duplicate_skips_model_and_embedding(self):
        with patch.object(semantic,'judge',AsyncMock()) as judge:
            result = await semantic.evaluate(importer([old()]),'group1',{'summary':'小明喜欢Python'},CONFIG)
        self.assertEqual(result.verdict,'duplicate')
        judge.assert_not_awaited()

    async def test_new_entities_do_not_bypass_model(self):
        with patch.object(semantic,'judge',AsyncMock(return_value=semantic.Decision('uncertain','关系需要核对'))) as judge:
            result = await semantic.evaluate(importer([old()]),'group1',{'summary':'小明喜欢Python','entities':['小明']},CONFIG)
        judge.assert_awaited_once()
        self.assertEqual(result.verdict,'uncertain')

    async def test_foreign_and_expired_memories_do_not_recall(self):
        rows = [old(chat='private1'),old(key='expired',expires_at=1),old(key='stale',metadata={'chat_id':'group1','memory_change':{'valid_to':1}})]
        with patch.object(semantic,'judge',AsyncMock()) as judge:
            result = await semantic.evaluate(importer(rows),'group1',{'summary':'小明喜欢Python'},CONFIG)
        self.assertEqual(result.verdict,'new')
        judge.assert_not_awaited()

    async def test_vector_recall_only_reads_existing_old_vectors(self):
        rows = [old('完全不同的字面说法',key='semantic'),old('小明喜欢Python',key='keyword')]
        reader = SimpleNamespace(get_vectors=lambda ids:{'semantic':np.array([1.,0.]),'keyword':np.array([0.,1.])})
        embed = SimpleNamespace(encode=AsyncMock(return_value=np.array([1.,0.])))
        imp = importer(rows); imp.vector_store=reader; imp.embedding_manager=embed
        hits,note = await semantic.recall('我喜欢Python',rows,imp,4)
        self.assertEqual(hits[0]['hash'],'semantic')
        self.assertEqual(hits[0]['vector_score'],1.0)
        self.assertIn('向量＋关键词',note)
        embed.encode.assert_awaited_once_with('我喜欢Python')

    async def test_embedding_failure_is_visible_and_still_needs_judgment(self):
        imp = importer([old()]); imp.vector_store=SimpleNamespace(get_vectors=lambda ids:{'old':np.array([1.,0.])})
        imp.embedding_manager=SimpleNamespace(encode=AsyncMock(side_effect=TimeoutError('test')))
        with patch.object(semantic,'judge',AsyncMock(return_value=semantic.Decision('conflict','偏好相反',['old'],0.95))):
            result = await semantic.evaluate(imp,'group1',{'summary':'小明不喜欢Python'},CONFIG)
        self.assertEqual(result.verdict,'conflict')
        self.assertIn('TimeoutError',result.recall_note)

    def test_high_similarity_does_not_force_duplicate(self):
        rows=[old()]
        result=semantic.parse({'verdict':'conflict','matched_ids':['old'],'confidence':0.95,
                               'reason':'当前偏好相反','new_text':''},'小明不喜欢Python',rows,0.9)
        self.assertEqual(result.verdict,'conflict')

    def test_invalid_id_and_hallucinated_new_content_rejected(self):
        values=[{'verdict':'duplicate','matched_ids':['unknown'],'confidence':0.99,'reason':'重复','new_text':''},
                {'verdict':'new','matched_ids':[],'confidence':0.99,'reason':'新增','new_text':'小明养猫'}]
        for value in values:
            with self.assertRaises(ValueError):
                semantic.parse(value,'小明喜欢Python',[old()],0.9)

    def test_low_confidence_goes_to_review(self):
        result=semantic.parse({'verdict':'duplicate','matched_ids':['old'],'confidence':0.8,'reason':'不太确定','new_text':''},
                              'Python是小明的爱好',[old()],0.9)
        self.assertEqual(result.verdict,'uncertain')

    def test_new_fragment_must_be_from_original(self):
        text='小明喜欢Python。小明周五参加编程社团。'
        result=semantic.parse({'verdict':'new','matched_ids':['old'],'confidence':0.99,'reason':'新增社团活动',
                               'new_text':'小明周五参加编程社团。'},text,[old()],0.9)
        self.assertEqual(result.new_text,'小明周五参加编程社团。')

    def test_partial_clause_cannot_lose_subject(self):
        text='小明喜欢Python，并计划周五参加编程社团。'
        result=semantic.parse({'verdict':'new','matched_ids':['old'],'confidence':0.95,'reason':'新增计划',
                               'new_text':'计划周五参加编程社团'},text,[old()],0.9)
        self.assertEqual(result.verdict,'uncertain')
        result=semantic.parse({'verdict':'new','matched_ids':['old'],'confidence':0.95,'reason':'新增计划',
                               'new_text':'他计划周五参加编程社团。'},'小明喜欢Python。他计划周五参加编程社团。',[old()],0.9)
        self.assertEqual(result.verdict,'uncertain')

    def test_invalid_confidence_and_conflicting_response_rejected(self):
        for confidence in (True,float('nan'),1.1,-0.1):
            with self.assertRaises(ValueError):
                semantic.parse({'verdict':'duplicate','matched_ids':['old'],'confidence':confidence,'reason':'重复','new_text':''},
                               '小明喜欢Python',[old()],0.9)
        with self.assertRaises(ValueError):
            semantic.parse({'verdict':'conflict','matched_ids':['old'],'confidence':1.0,'reason':'冲突','new_text':'偷偷写入'},
                           '小明喜欢Python',[old()],0.9)

    def test_log_explains_recall_separately_from_decision(self):
        event={'kind':'摘要去重判断','seq':1,'time':'2026-10-01T12:00:00+08:00'}
        data={'status':'确认重复','new_memory':'Python是小明的爱好','old_memories':['小明喜欢Python'],
              'candidate_memories':['小明喜欢Python'],'confidence':0.95,
              'retrieval_scores':[{'vector':0.97}],'reason':'同一偏好且无新增','recall_note':'向量＋关键词'}
        text=render_event(event,data)
        self.assertIn('用于召回，不是同义概率',text)
        self.assertIn('AI判断把握：95%',text)
        self.assertIn('本次比较候选',text)

    async def test_model_failure_goes_to_review(self):
        with patch.object(semantic,'judge',AsyncMock(side_effect=ValueError('模型JSON损坏'))):
            result=await semantic.evaluate(importer([old()]),'group1',{'summary':'Python是小明的爱好'},CONFIG)
        self.assertEqual(result.verdict,'uncertain')
        self.assertIn('ValueError',result.reason)

    async def test_history_changes_during_model_wait_requires_review(self):
        rows=[old()]
        async def change(*args):
            rows[0]=old('小明不喜欢Python')
            return semantic.Decision('duplicate','重复',['old'],0.99)
        with patch.object(semantic,'judge',change):
            result=await semantic.evaluate(importer(rows),'group1',{'summary':'Python是小明的爱好'},CONFIG)
        self.assertEqual(result.verdict,'uncertain')
        self.assertIn('已变化',result.reason)

    async def test_model_timeout_cancels_wait(self):
        import asyncio
        done=asyncio.Event()
        async def slow(*args):
            try:
                await asyncio.sleep(10)
            finally:
                done.set()
        with patch.object(semantic,'judge',slow):
            result=await semantic.evaluate(importer([old()]),'group1',{'summary':'Python是小明的爱好'},
                                           {**CONFIG,'timeout_seconds':0.01})
        self.assertEqual(result.verdict,'uncertain')
        self.assertTrue(done.is_set())

    async def test_disabled_mode_still_never_uses_substring_rule(self):
        result=await semantic.evaluate(importer([old('不喜欢每天晚上花两个小时学习Python')]),'group1',
                                       {'summary':'喜欢每天晚上花两个小时学习Python'},{**CONFIG,'enabled':False})
        self.assertEqual(result.verdict,'new')

    def store(self):
        return AuditStore(Path(__file__).resolve().parents[2]/'.runtime/test-data'/uuid.uuid4().hex)

    async def test_capture_is_durable_and_only_admin_can_approve(self):
        store=self.store()
        decision=semantic.Decision('uncertain','模型超时',candidates=[old()])
        with patch('heart_shared.storage.AuditStore',return_value=store):
            cid=semantic.capture(None,'group1',{'summary':'Python是小明的爱好'},decision)
            self.assertEqual(cid,semantic.capture(None,'group1',{'summary':'Python是小明的爱好'},decision))
        backend=SimpleNamespace(summary_snapshot_matches=AsyncMock(return_value=True),
            write_reviewed_candidate=AsyncMock(return_value={'success':True,'stored_ids':['written']}))
        inbox=CandidateInbox(store,backend)
        text=await inbox.resolve('summary:group1',cid,True)
        self.assertIn('只能由WebUI',text)
        backend.write_reviewed_candidate.assert_not_awaited()
        text=await inbox.review(cid,True,'Python是小明的爱好')
        self.assertIn('写入成功',text)
        with store.connect() as db:
            self.assertEqual(db.execute('SELECT status FROM heart_candidates WHERE id=?',(cid,)).fetchone()[0],'written')

    async def test_conflict_summary_cannot_be_blindly_approved(self):
        store=self.store()
        with patch('heart_shared.storage.AuditStore',return_value=store):
            cid=semantic.capture(None,'group1',{'summary':'小明不喜欢Python'},semantic.Decision('conflict','相反',candidates=[old()]))
        backend=SimpleNamespace(write_reviewed_candidate=AsyncMock())
        inbox=CandidateInbox(store,backend)
        text=await inbox.review(cid,True,'小明不喜欢Python')
        self.assertIn('不能直接批准覆盖',text)
        backend.write_reviewed_candidate.assert_not_awaited()

    async def test_stale_snapshot_cannot_be_approved(self):
        store=self.store()
        with patch('heart_shared.storage.AuditStore',return_value=store):
            cid=semantic.capture(None,'group1',{'summary':'Python是小明的爱好'},semantic.Decision('uncertain','不确定',candidates=[old()]))
        backend=SimpleNamespace(summary_snapshot_matches=AsyncMock(return_value=False),write_reviewed_candidate=AsyncMock())
        text=await CandidateInbox(store,backend).review(cid,True,'Python是小明的爱好')
        self.assertIn('已改变',text)
        backend.write_reviewed_candidate.assert_not_awaited()

    async def test_importer_candidate_skip_and_partial_new_graph_filter(self):
        imp=SummaryImporter(None,None,SimpleNamespace(),None,{})
        imp._existing_summary_result=lambda **kwargs:None
        imp._ensure_runtime_self_check=AsyncMock(return_value=(True,'ok'))
        imp._build_previous_summary_context=lambda *args,**kwargs:''
        imp._resolve_summary_model_task=lambda:('memory',SimpleNamespace(model_list=['mock']))
        imp._execute_import=AsyncMock(return_value='new-id'); imp._persist_import=lambda:None
        data={'summary':'小明喜欢Python。小红参加围棋社团。','entities':['小明','Python','小红','围棋社团'],
              'relations':[{'subject':'小明','predicate':'喜欢','object':'Python'},
                           {'subject':'小红','predicate':'参加','object':'围棋社团'}]}
        response=SimpleNamespace(success=True,completion=SimpleNamespace(response=json.dumps(data)))
        with (patch('src.A_memorix.core.utils.summary_importer.message_api.get_messages_by_time_in_chat',return_value=[SimpleNamespace(timestamp=1)]),
              patch('src.A_memorix.core.utils.summary_importer.message_api.build_readable_messages',return_value='隔离对话'),
              patch('src.A_memorix.core.utils.summary_importer.llm_api.generate',AsyncMock(return_value=response)),
              patch.object(semantic,'record_decision'),patch.object(semantic,'capture',return_value=88) as capture,
              patch.object(semantic,'evaluate',AsyncMock(return_value=semantic.Decision('uncertain','超时')))):
            result=await imp._import_from_stream_unlocked('group1',metadata={})
            self.assertTrue(result.skipped); self.assertIn('候选记忆 #88',result.detail)
            capture.assert_called_once(); imp._execute_import.assert_not_awaited()
        with (patch('src.A_memorix.core.utils.summary_importer.message_api.get_messages_by_time_in_chat',return_value=[SimpleNamespace(timestamp=1)]),
              patch('src.A_memorix.core.utils.summary_importer.message_api.build_readable_messages',return_value='隔离对话'),
              patch('src.A_memorix.core.utils.summary_importer.llm_api.generate',AsyncMock(return_value=response)),
              patch.object(semantic,'record_decision'),
              patch.object(semantic,'evaluate',AsyncMock(return_value=semantic.Decision('new','新增小红活动',new_text='小红参加围棋社团。')))):
            result=await imp._import_from_stream_unlocked('group1',metadata={})
            args=imp._execute_import.await_args.args
            self.assertEqual(args[0],'小红参加围棋社团。')
            self.assertEqual(args[1],['小红','围棋社团'])
            self.assertEqual(len(args[2]),1)
