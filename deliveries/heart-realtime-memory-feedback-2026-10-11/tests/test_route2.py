from types import SimpleNamespace
from unittest.mock import patch
import asyncio
import unittest

from support import engine, drafts, host, Model, Generator, Item, native_modules


class DraftTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.model=Model();self.generator=Generator(self.model)
        self.runtime=SimpleNamespace(_chat_history=['过去已发生的对话'])
        self.lease=host.Lease('visitor','connection',epoch=1,active=True,preemptive=True,
                              utterance='u1',native_id='formal1')
        self.session=SimpleNamespace(session_id='session')
        host.LEASES['session']=self.lease
        self.events=[]
        self.patch=patch.dict('sys.modules',native_modules(self.runtime,self.generator));self.patch.start()
        self.auditpatch=patch.object(drafts,'audit',lambda s,l,p,**d:self.events.append((p,d)));self.auditpatch.start()
    async def asyncTearDown(self):
        await drafts.cancel_prepared('session',self.lease,'test end')
        self.auditpatch.stop();self.patch.stop();host.LEASES.clear()
    async def prepare(self,text='今天过得怎么样呢'):
        return await drafts.prepare(self.session,self.lease,{'epoch':1,'utterance_id':'u1',
            'text':text,'evidence_key':'','evidence':None})
    async def generate(self,text='今天过得怎么样呢',sid='session',**overrides):
        async def factory(client,model_info=None):return [Item('正常提示词')]
        args=dict(session_id=sid,generator=self.generator,model=self.model,context_factory=factory,
            options=None,reply_message=SimpleNamespace(message_id='formal1',processed_plain_text=text),
            requirements=self.generator.extra,tool_args={},task_name='replyer',model_name=None,retry_count=0)
        args.update(overrides)
        return await drafts.generate_reply(**args)
    async def test_reuse_saves_main_replyer_call(self):
        await self.prepare();self.lease.prepared.approved=True
        result=await self.generate()
        self.assertEqual(len(self.model.calls),1);self.assertEqual(result.response,'原角色回复。1')
        self.assertTrue(any(p=='adopted' for p,d in self.events))
        self.assertEqual(result.total_tokens,0)
        self.assertEqual(self.lease.reply_evidence_key,'')
    async def test_discard_changed_final(self):
        await self.prepare();self.lease.prepared.approved=True
        await self.generate('今天过得不怎么样呢')
        self.assertEqual(len(self.model.calls),2)
        self.assertTrue(any(p=='discarded' and '转写' in d['reason'] for p,d in self.events))
    async def test_new_mood_rejects_cached_reply(self):
        await self.prepare();self.lease.prepared.approved=True;self.generator.extra='已确认心情：生气'
        await self.generate();self.assertEqual(len(self.model.calls),2)
    async def test_new_history_rejects_cached_reply(self):
        await self.prepare();self.lease.prepared.approved=True;self.runtime._chat_history.append('另一个正式事件')
        await self.generate();self.assertEqual(len(self.model.calls),2)
    async def test_tool_reference_uses_native_generation(self):
        await self.prepare();self.lease.prepared.approved=True
        await self.generate(tool_args={'reply_reference':'原生工具查到了新事实'})
        self.assertEqual(len(self.model.calls),2)
    async def test_normal_style_without_extra_condition_can_reuse(self):
        await self.prepare();self.lease.prepared.approved=True
        await self.generate(tool_args={'reply_style':'正常回复'})
        self.assertEqual(len(self.model.calls),1)
    async def test_planner_thought_reference_rejects_draft(self):
        await self.prepare();self.lease.prepared.approved=True
        await self.generate(reply_reason='应先澄清用户的日期，不能直接回答。')
        self.assertEqual(len(self.model.calls),2)
    async def test_keyword_rule_changed_rejects_draft(self):
        await self.prepare();self.lease.prepared.approved=True
        self.generator._build_keyword_reaction_prompt=lambda **kwargs:'检测到正式输入关键词'
        await self.generate();self.assertEqual(len(self.model.calls),2)
    async def test_item_hook_rewrite_rejects_draft(self):
        await self.prepare();self.lease.prepared.approved=True
        await self.generate(expected_items=[Item('其他插件改写前')])
        self.assertEqual(len(self.model.calls),2)
    async def test_unbound_qq_and_normal_webui_unchanged(self):
        for sid in ['qq-group','ordinary-webui']:await self.generate(sid=sid)
        self.assertEqual(len(self.model.calls),2);self.assertEqual(self.events,[])
    async def test_same_draft_only_consumed_once(self):
        await self.prepare();self.lease.prepared.approved=True
        await self.generate();await self.generate();self.assertEqual(len(self.model.calls),2)
    async def test_disabled_mode_cannot_prepare(self):
        self.lease.preemptive=False
        with self.assertRaises(ValueError):await self.prepare()
        self.assertEqual(self.model.calls,[])
    async def test_late_first_query_restarts_inflight_generation(self):
        self.model.block_first=True
        work=asyncio.create_task(self.generate())
        while not self.model.calls:await asyncio.sleep(.001)
        event=self.lease.evidence_changed
        self.lease.evidence_key='new';self.lease.evidence={'local':{'items':[{'key':'考试','value':'周四'}]}}
        self.lease.evidence_changed=asyncio.Event();event.set()
        result=await asyncio.wait_for(work,1)
        self.assertEqual(len(self.model.calls),2);self.assertEqual(self.model.cancelled,1)
        self.assertIn('周四',''.join(self.model.calls[1]));self.assertEqual(result.response,'原角色回复。2')
        self.assertTrue(any(p=='regenerated' for p,d in self.events))
    async def test_second_evidence_change_does_not_generate_forever(self):
        self.model.block_all=True;work=asyncio.create_task(self.generate())
        for key in ['one','two']:
            while len(self.model.calls)<(1 if key=='one' else 2):await asyncio.sleep(.001)
            event=self.lease.evidence_changed;self.lease.evidence_key=key
            self.lease.evidence_changed=asyncio.Event();event.set()
        with self.assertRaises(RuntimeError):await asyncio.wait_for(work,1)
        self.assertEqual(len(self.model.calls),2)
    async def test_disconnect_cancels_model_task(self):
        self.model.block_all=True;work=asyncio.create_task(self.generate())
        while not self.model.calls:await asyncio.sleep(.001)
        work.cancel()
        with self.assertRaises(asyncio.CancelledError):await work
        self.assertEqual(self.model.cancelled,1)


class EngineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.calls=[];self.events=[];self.packets=[];self.hold_read=asyncio.Event();self.hold_read.set()
        self.commit_fail=False;self.local_hold=None
        async def call(name,**kw):
            self.calls.append((name,kw))
            if name=='local.facts':
                if self.local_hold:await self.local_hold.wait()
                return {'items':[{'key':'考试','value':'周四','version':'1'}]}
            if kw['action']=='read':await self.hold_read.wait();return {'success':True,'items':[]}
            if kw['action']=='begin':return {'success':True,'local_cancelled':True}
            if kw['action']=='speculate':return {'success':True,'output':'准备好的原角色草稿。'}
            if kw['action']=='commit' and self.commit_fail:raise OSError('临时写入失败')
            return {'success':True}
        async def emit(p):self.packets.append(p)
        self.c=engine.Conversation('session','visitor','connection',call,emit,
            lambda f,s,**d:self.events.append((f,d)))
        self.c.stable_seconds=.02;self.c.query_wait_seconds=.02;self.c.query_timeout_seconds=.6
        await self.c.begin('u1',audio=True)
    async def asyncTearDown(self):await self.c.close()
    def count(self,action):return sum(k.get('action')==action for n,k in self.calls)
    async def test_off_does_not_call_speculative_model(self):
        await self.c.partial_input('u1',1,'今天过得怎么样呢');await asyncio.sleep(.04)
        self.assertEqual(self.count('speculate'),0)
    async def test_on_stable_input_prepares_one_hidden_draft(self):
        await self.c.set_mode(True);await self.c.partial_input('u1',1,'今天过得怎么样呢')
        await asyncio.wait_for(self.c.spec_timer,.5)
        self.assertEqual(self.count('speculate'),1);self.assertEqual(self.count('activate'),0)
        self.assertFalse(self.c.finals)
        await self.c.partial_input('u1',2,'今天过得怎么样呢');await asyncio.sleep(.04)
        self.assertEqual(self.count('speculate'),1)
    async def test_revision_resets_stability_timer(self):
        await self.c.set_mode(True);self.c.stable_seconds=.06
        await self.c.partial_input('u1',1,'今天过得怎么样呢');await asyncio.sleep(.02)
        await self.c.partial_input('u1',2,'今天过得怎么样呢我还想问问');await asyncio.sleep(.03)
        self.assertEqual(self.count('speculate'),0)
    async def test_changed_input_discards_without_more_speculation(self):
        await self.c.set_mode(True);await self.c.partial_input('u1',1,'今天过得怎么样呢')
        for _ in range(100):
            if self.count('speculate'):break
            await asyncio.sleep(.01)
        await self.c.partial_input('u1',2,'今天其实过得不怎么样呢');await asyncio.sleep(.04)
        self.assertEqual(self.count('speculate'),1);self.assertEqual(self.count('spec_cancel'),1)
    async def test_effectful_input_never_speculated(self):
        await self.c.set_mode(True)
        for i,t in enumerate(['提醒我明天参加考试','记住我明天参加考试','请运行一个程序吧','因为','如果']):
            await self.c.partial_input('u1',i+1,t);await asyncio.sleep(.025)
        self.assertEqual(self.count('speculate'),0)
    async def test_typed_input_never_speculated(self):
        await self.c.begin('u2',audio=False);await self.c.set_mode(True)
        await self.c.partial_input('u2',1,'今天过得怎么样呢');await asyncio.sleep(.04)
        self.assertEqual(self.count('speculate'),0)
    async def test_wait_timeout_keeps_query_and_admits_first_late_result(self):
        self.hold_read.clear();await self.c.partial_input('u1',1,'查一下考试日期')
        await self.c.final('u1','查一下考试日期')
        self.assertTrue(any(d['phase']=='wait_budget_exceeded' for f,d in self.events))
        self.assertFalse(self.c.task.done());self.hold_read.set();await asyncio.wait_for(self.c.task,1)
        self.assertTrue(any(d['phase']=='late_adopted' for f,d in self.events))
        contexts=[k['text'] for n,k in self.calls if k.get('action')=='context']
        self.assertIn('周四',contexts[-1]);self.assertGreaterEqual(self.count('evidence'),2)
    async def test_entire_query_deadline_includes_local_read(self):
        self.c.query_timeout_seconds=.03;self.local_hold=asyncio.Event()
        with self.assertRaises(asyncio.TimeoutError):await self.c.read('考试')
    async def test_timeout_logged_as_timeout_not_success(self):
        self.c.query_timeout_seconds=.03;self.hold_read.clear()
        await self.c.partial_input('u1',1,'查一下考试日期');await asyncio.sleep(.33)
        self.assertTrue(any(d['phase']=='timeout' for f,d in self.events));self.assertIsNone(self.c.result)
    async def test_late_result_with_queued_output_freezes_and_supplements(self):
        await self.c.final('u1','查一下考试日期')
        clip=await self.c.register_clip('native-draft','还没查到日期。','native-draft',0,self.c.epoch)
        value={'native':{'items':[]},'local':{'items':[{'key':'考试','value':'周四'}]}}
        await self.c.admit_late_result(None,value,self.c.epoch,'u1')
        self.assertEqual(self.c.clips[clip['clip_id']].state,'stopped')
        correction=[p for p in self.packets if p['type']=='heart-rt-correction'][-1]
        self.assertEqual(correction['evidence']['mode'],'supplement')
    async def test_late_result_after_generation_before_first_clip_freezes_old_draft(self):
        await self.c.final('u1','查一下考试日期')
        value={'native':{'items':[]},'local':{'items':[{'key':'考试','value':'周四'}]}}
        old_epoch=self.c.epoch
        await self.c.admit_late_result(None,value,old_epoch,'u1',reply_ready_stale=True)
        self.assertGreater(self.c.epoch,old_epoch)
        self.assertTrue(any(p['type']=='heart-rt-correction' for p in self.packets))
    async def test_failed_commit_can_retry_same_receipt(self):
        clip=await self.c.register_clip('native','完整句子。','native',0,self.c.epoch,audio=False)
        self.commit_fail=True
        with self.assertRaises(OSError):await self.c.receipt(clip['clip_id'],self.c.epoch,'visible')
        self.commit_fail=False
        await self.c.receipt(clip['clip_id'],self.c.epoch,'visible');await self.c.receipt(clip['clip_id'],self.c.epoch,'visible')
        self.assertEqual(self.count('commit'),2)
    async def test_mode_logged_on_and_off(self):
        await self.c.set_mode(True);await self.c.set_mode(False)
        rows=[d for f,d in self.events if f=='插件开关']
        self.assertEqual([x['preemptive_enabled'] for x in rows],[True,False])


if __name__=='__main__':unittest.main(verbosity=2)
