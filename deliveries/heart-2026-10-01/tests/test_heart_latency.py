"""性能回归：无付费请求、无真实聊天数据、无QQ连接。"""
import asyncio
import json
import logging
import time
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import patch

from test_heart_mood import ROOT, MoodEngine, plugin
from test_mood_plugin.appraiser import AIAppraiser
from heart_shared import storage
from heart_shared.storage import AuditStore
from heart_llm_budget import generate_with_budget


class IncrementalLogs(unittest.TestCase):
    def setUp(self):
        self.store = AuditStore(ROOT/'.runtime/test-data'/uuid.uuid4().hex)
        self.store.append('收到对话', 'synthetic', text='第1条')

    def file(self):
        return next((self.store.root/'logs').rglob('*.txt'))

    def test_only_new_event_rendered_and_rebuild_identical(self):
        for i in range(20):
            self.store.append('收到对话', 'synthetic', text=str(i))
        with patch.object(storage, 'render_event', wraps=storage.render_event) as render:
            self.store.append('收到对话', 'synthetic', text='最后一条')
            self.assertEqual(render.call_count, 1)
        before = self.file().read_bytes()
        self.store.rebuild()
        self.assertEqual(before, self.file().read_bytes())
        self.assertEqual(before.count(b'\xef\xbb\xbf'), 1)

    def test_rollback_export_repaired_without_phantom_event(self):
        with self.assertRaises(RuntimeError):
            with self.store.transaction() as db:
                self.store.append_in(db, '收到对话', 'synthetic', {'text':'回滚事件不能留存'})
                raise RuntimeError('模拟提交前中断')
        self.store.append('收到对话', 'synthetic', text='事务已恢复')
        text = self.file().read_text(encoding='utf-8-sig')
        self.assertNotIn('回滚事件不能留存', text)
        self.assertIn('事务已恢复', text)

    def test_missing_file_rebuilt_and_duplicate_not_appended(self):
        self.file().unlink()
        self.store.append('收到对话', 'synthetic', event_id='same', text='去重事件')
        before = self.file().read_bytes()
        self.store.append('收到对话', 'synthetic', event_id='same', text='去重事件')
        self.assertEqual(before, self.file().read_bytes())
        self.assertIn('第1条', before.decode('utf-8-sig'))

    def test_multiple_store_instances_use_committed_cursor(self):
        second = AuditStore(self.store.root)
        second.append('收到对话', 'synthetic', text='另一个插件')
        self.store.append('收到对话', 'synthetic', text='原插件')
        before = self.file().read_bytes()
        second.rebuild()
        self.assertEqual(before, self.file().read_bytes())


class RequestBudget(unittest.IsolatedAsyncioTestCase):
    async def test_timeout_cancels_host_request_and_awaits_cleanup(self):
        cleaned = asyncio.Event()
        async def generate(request):
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()
        start = time.monotonic()
        with self.assertRaises(TimeoutError):
            await generate_with_budget(generate, object(), 1)
        self.assertTrue(cleaned.is_set())
        self.assertLess(time.monotonic()-start, 2)

    async def test_old_call_unchanged_and_invalid_budget_never_starts(self):
        calls=[]
        async def generate(request):
            calls.append(request)
            return request
        self.assertEqual(await generate_with_budget(generate, 'old'), 'old')
        for seconds in (True,0,121,float('nan'),'20'):
            with self.assertRaises(ValueError):
                await generate_with_budget(generate,'invalid',seconds)
        self.assertEqual(calls,['old'])


class MoodLatency(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store=AuditStore(ROOT/'.runtime/test-data'/uuid.uuid4().hex)
        self.cfg=plugin.Config(ai=plugin.AISettings(enabled=True),emotions=plugin.TopicEmotions(enabled=True))
        self.calls=[]
        async def failed(capability, **kwargs):
            self.calls.append(kwargs)
            return {'success':False,'error_code':'request_timeout'}
        self.p=SimpleNamespace(store=self.store,engine=MoodEngine(self.store),config=self.cfg,
                               ctx=SimpleNamespace(call_capability=failed),_get_logger=lambda:logging.getLogger('test'))
        self.ai=AIAppraiser(self.p)

    async def asyncTearDown(self):
        await self.ai.close()

    def message(self, mid):
        return {'session_id':'synthetic','message_id':str(mid),'processed_plain_text':'普通测试文本',
                'message_info':{'user_info':{'user_id':'fake','user_nickname':'虚构测试'}}}

    async def finish(self, mid):
        await self.ai.submit(self.message(mid))
        await asyncio.wait_for(self.ai.queue.join(),3)

    async def test_budget_reaches_host_separately_from_rpc_and_pause_recovers(self):
        for i in range(3):
            await self.finish(i)
        self.assertEqual(len(self.calls),2)
        self.assertEqual(self.calls[0]['request_timeout_seconds'],20)
        self.assertEqual(self.calls[0]['timeout_ms'],22000)
        with self.store.connect() as db:
            state=self.store.mood_snapshot(db,'synthetic')
        self.assertEqual(state['assessment']['method'],'拥堵保护未评估')
        self.assertEqual(state['stimulus_delta'],0)
        self.ai.pause_until=0
        await self.finish(3)
        self.assertEqual(len(self.calls),3)

    async def test_pause_can_be_disabled(self):
        self.cfg.performance.failure_pause_seconds=0
        for i in range(3):
            await self.finish(i)
        self.assertEqual(len(self.calls),3)

    async def test_reply_first_does_not_wait_for_pending_ai(self):
        instance=plugin.create_plugin()
        instance.set_plugin_config(self.cfg.model_dump())
        instance.store=self.store;instance.engine=self.p.engine;instance.appraiser=self.ai
        self.ai.pending.add(('synthetic','pending'))
        start=time.monotonic()
        result=await asyncio.wait_for(instance.before_reply(session_id='synthetic'),1)
        self.assertLess(time.monotonic()-start,1)
        self.assertIn('本轮情绪评估还未完成',result['modified_kwargs']['extra_prompt'])
        with self.store.connect() as db:
            data=json.loads(db.execute("SELECT payload FROM events WHERE kind='心情影响回复风格'").fetchone()[0])
        self.assertTrue(data['reply_first'])
        self.assertLess(data['emotion_wait_ms'],100)

    async def test_old_wait_mode_can_be_selected(self):
        instance=plugin.create_plugin()
        instance.set_plugin_config(self.cfg.model_dump())
        instance.config.performance.reply_first=False
        instance.config.emotions.reply_wait_seconds=.1
        instance.store=self.store;instance.engine=self.p.engine;instance.appraiser=self.ai
        self.ai.pending.add(('synthetic','pending'))
        start=time.monotonic()
        await instance.before_reply(session_id='synthetic')
        self.assertGreaterEqual(time.monotonic()-start,.09)


class InstallerPreflight(unittest.TestCase):
    def test_new_delivery_preflight_keeps_unknown_host_untouched(self):
        import importlib.util
        spec=importlib.util.spec_from_file_location('latency_installer',ROOT/'install_delivery.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        staging=ROOT/'.runtime/test-data'/uuid.uuid4().hex
        dest=staging/'src/plugin_runtime/capabilities/core.py'
        dest.parent.mkdir(parents=True)
        original='# 同伴独立修改的模型接口\n'
        dest.write_text(original,encoding='utf-8')
        rows=module.plan(staging,include_native=True)
        with self.assertRaisesRegex(ValueError,'未知'):
            module.apply(staging,rows,True,True)
        self.assertEqual(dest.read_text(encoding='utf-8'),original)


class GroupBatchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store=AuditStore(ROOT/'.runtime/test-data'/uuid.uuid4().hex)
        self.cfg=plugin.Config(ai=plugin.AISettings(enabled=True),emotions=plugin.TopicEmotions(enabled=True))
        self.calls=[]
        async def generate(capability,**kwargs):
            self.calls.append(kwargs)
            result={'delta':3,'confidence':.9,'reason':'整段友善互动','evidence':'谢谢你',
                    'emotion':{'action':'set','label':'开心','intensity':.5,'topic':'支持学习',
                               'reason':'收到友善回应','confidence':.9,'evidence':'谢谢你'},
                    'display':{'action':'none','label':'','reason':'没有表演请求','confidence':.9,'evidence':''}}
            return {'success':True,'response':json.dumps(result,ensure_ascii=False)}
        self.p=SimpleNamespace(store=self.store,engine=MoodEngine(self.store),config=self.cfg,
                               ctx=SimpleNamespace(call_capability=generate),_get_logger=lambda:logging.getLogger('test'))
        self.ai=AIAppraiser(self.p)

    async def asyncTearDown(self):
        await self.ai.close()

    def message(self, mid, session='group', group=True):
        info={'platform':'qq' if group else 'webui','user_info':{'user_id':'fake-'+str(mid),'user_nickname':'同学'+str(mid)}}
        if group:
            info['group_info']={'group_id':session,'group_name':'合成测试群'}
        return {'session_id':session,'message_id':str(mid),'processed_plain_text':'谢谢你，测试发言'+str(mid),'message_info':info}

    async def finish_group(self, session='group'):
        await self.ai.flush_group(session)
        await asyncio.wait_for(self.ai.queue.join(),3)

    async def test_eight_group_messages_one_call_all_logged_and_deduped(self):
        for i in range(8):
            msg=self.message(i)
            self.store.record_message(msg)
            await self.ai.submit(msg)
        await asyncio.wait_for(self.ai.queue.join(),3)
        self.assertEqual(len(self.calls),1)
        self.assertIn('本批会话发言',self.calls[0]['prompt'])
        with self.store.connect() as db:
            state=self.store.mood_snapshot(db,'group')
            self.assertEqual(state['value'],53)
            self.assertEqual(state['batch_count'],8)
            self.assertEqual(state['trigger']['user'],'')
            self.assertEqual(db.execute('SELECT count(*) FROM mood_processed').fetchone()[0],8)
            self.assertEqual(db.execute("SELECT count(*) FROM events WHERE kind='收到对话'").fetchone()[0],8)
            mood=json.loads(db.execute("SELECT payload FROM events WHERE kind='心情变化'").fetchone()[0])
            emotion=json.loads(db.execute("SELECT payload FROM events WHERE kind='临时情绪产生'").fetchone()[0])
            self.assertEqual(len(mood['dialogue']),8)
            self.assertEqual(len(emotion['dialogue']),8)
        self.assertFalse(self.ai.pending)
        for i in range(8):
            await self.ai.submit(self.message(i))
        self.assertEqual(len(self.calls),1)
        self.assertFalse(self.ai.group_buffers)

    async def test_private_stays_individual_and_sessions_never_mix(self):
        await self.ai.submit(self.message(1,'group-a'))
        await self.ai.submit(self.message(2,'group-b'))
        await self.ai.submit(self.message(3,'private',False))
        await asyncio.wait_for(self.ai.queue.join(),3)
        self.assertEqual(len(self.calls),1)
        self.assertNotIn('本批会话发言',self.calls[0]['prompt'])
        await self.finish_group('group-a');await self.finish_group('group-b')
        self.assertEqual(len(self.calls),3)
        with self.store.connect() as db:
            for session in ('group-a','group-b'):
                state=self.store.mood_snapshot(db,session)
                self.assertEqual(state['batch_count'],1)

    async def test_batch_cooldown_is_per_group_not_last_speaker(self):
        await self.ai.submit(self.message(1));await self.ai.submit(self.message(2))
        await self.finish_group()
        await self.ai.submit(self.message(3));await self.ai.submit(self.message(4))
        await self.finish_group()
        with self.store.connect() as db:
            state=self.store.mood_snapshot(db,'group')
        self.assertEqual(state['stimulus_delta'],0)
        self.assertIn('会话合并评分',state['reason'])

    async def test_timer_flushes_and_close_cancels_unsubmitted_batch(self):
        self.cfg.performance.group_batch_seconds=.05
        await self.ai.submit(self.message(1))
        self.assertTrue(await self.ai.wait_session('group',2))
        self.assertEqual(len(self.calls),1)
        self.cfg.performance.group_batch_seconds=3
        await self.ai.submit(self.message(2))
        await self.ai.close()
        self.assertFalse(self.ai.pending)
        self.assertFalse(self.ai.group_buffers)
        self.assertEqual(len(self.calls),1)

    async def test_batch_switch_off_restores_individual(self):
        self.cfg.performance.group_batch_seconds=0
        await self.ai.submit(self.message(1));await self.ai.submit(self.message(2))
        await asyncio.wait_for(self.ai.queue.join(),3)
        self.assertEqual(len(self.calls),2)
        self.assertFalse(self.ai.group_buffers)

    async def test_private_threshold_and_deadline_are_per_session(self):
        self.cfg.performance.private_batch_max_messages=3
        self.cfg.performance.private_batch_seconds=1
        await self.ai.submit(self.message(1,'private-a',False))
        await self.ai.submit(self.message(2,'private-a',False))
        self.assertEqual(len(self.calls),0)
        await self.ai.submit(self.message(3,'private-a',False))
        await asyncio.wait_for(self.ai.queue.join(),3)
        self.assertEqual(len(self.calls),1)
        with self.store.connect() as db:
            self.assertEqual(self.store.mood_snapshot(db,'private-a')['batch_count'],3)
        await self.ai.submit(self.message(4,'private-b',False))
        self.assertTrue(await self.ai.wait_session('private-b',2))
        self.assertEqual(len(self.calls),2)
        with self.store.connect() as db:
            self.assertEqual(self.store.mood_snapshot(db,'private-b')['batch_count'],1)


if __name__=='__main__':
    unittest.main()
