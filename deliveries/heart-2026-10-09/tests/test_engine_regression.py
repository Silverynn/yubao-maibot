from pathlib import Path
import asyncio
import importlib.util
import json
import sys
import unittest

ROOT=Path(__file__).resolve().parents[1]
from support import engine


class Native:
    def __init__(self):
        self.calls=[]
        self.facts={'items':[{'key':'meeting','value':'16:00','version':'1'}]}
        self.read_started=asyncio.Event()
        self.read_release=None
        self.fail=False
    async def __call__(self,name,**kwargs):
        self.calls.append((name,kwargs))
        if name=='local.facts':
            return json.loads(json.dumps(self.facts))
        if kwargs['action']=='begin':
            return {'success':True,'local_cancelled':True}
        if kwargs['action']=='read':
            self.read_started.set()
            if self.read_release:
                await self.read_release.wait()
            if self.fail:
                raise OSError('隔离故障')
            return {'success':True,'items':[],'version':'mem1'}
        return {'success':True}


class EngineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.native=Native()
        self.events=[]
        self.packets=[]
        async def emit(packet):self.packets.append(packet)
        self.c=engine.Conversation('registered-native-test-session','test-visitor',
            'test-connection',self.native,emit,lambda feature,session,**p:self.events.append((feature,p)))
        self.c.character_names=['鱼宝']  # 测试角色，不是插件默认配置。
        await self.c.begin('u1')
    async def asyncTearDown(self):
        await self.c.close()
    async def test_partial_does_read_work_before_final_and_does_not_commit(self):
        await self.c.partial_input('u1',1,'查一下会议时间')
        await asyncio.wait_for(self.native.read_started.wait(),1)
        self.assertNotIn('u1',self.c.finals)
        self.assertFalse(any(k['action']=='activate' for n,k in self.native.calls if n=='heart.realtime'))
        self.assertTrue(any(f=='边听边查' and p['phase']=='started' for f,p in self.events))
    async def test_final_idempotency_and_text_preserved(self):
        text='嗯，还在啊。你可以继续说，别着急。'
        first=await self.c.final('u1',text)
        again=await self.c.final('u1',text)
        self.assertFalse(first['duplicate'])
        self.assertTrue(again['duplicate'])
        self.assertEqual(sum(p['phase']=='accepted' for f,p in self.events if f=='正式输入'),1)
        self.assertIn(text,[p.get('text') for f,p in self.events])
    async def test_changed_final_same_id_rejected(self):
        await self.c.final('u1','先说这个。')
        with self.assertRaises(ValueError):await self.c.final('u1','改成别的。')
    async def test_old_utterance_rejected(self):
        await self.c.begin('u2')
        await self.c.partial_input('u1',9,'查一下旧的会议')
        with self.assertRaises(ValueError):await self.c.final('u1','旧的输入')
        self.assertFalse(self.c.query)
    async def test_old_revision_cannot_change_current_query(self):
        await self.c.partial_input('u1',3,'查一下会议')
        await self.c.partial_input('u1',2,'查一下别的')
        self.assertEqual(self.c.query,'会议')
    async def test_effectful_partial_never_runs(self):
        for i,text in enumerate(['记住我的会议时间','提醒我查一下会议','不要查一下会议','删除之前的记忆']):
            await self.c.partial_input('u1',i+1,text)
        await asyncio.sleep(.3)
        self.assertFalse(self.native.read_started.is_set())
    async def test_topic_change_cancels_old_task(self):
        self.native.read_release=asyncio.Event()
        await self.c.partial_input('u1',1,'查一下会议')
        await asyncio.wait_for(self.native.read_started.wait(),1)
        old=self.c.task
        await self.c.partial_input('u1',2,'不查了，聊别的')
        await asyncio.sleep(.05)
        self.assertTrue(old.cancelled())
        self.assertIsNone(self.c.result)
    async def test_late_result_after_new_turn_rejected(self):
        captured_epoch=self.c.epoch
        captured_ticket=self.c.query_ticket
        await self.c.begin('u2')
        await self.c.prefetch(captured_epoch,'u1','会议',captured_ticket)
        self.assertIsNone(self.c.result)
        self.assertTrue(any(p['phase']=='discarded' for f,p in self.events if f=='边听边查'))
    async def test_fast_stop_has_no_llm_and_not_repeat(self):
        epoch=self.c.epoch
        await self.c.partial_input('u1',1,'停一下')
        self.assertGreater(self.c.epoch,epoch)
        after=self.c.epoch
        await self.c.partial_input('u1',2,'停一下')
        self.assertEqual(self.c.epoch,after)
        self.assertFalse(any(n.startswith('llm') for n,_ in self.native.calls))
    async def test_quoted_or_negative_stop_is_not_stop(self):
        epoch=self.c.epoch
        await self.c.partial_input('u1',1,'他说“停一下”，但我想继续')
        await self.c.partial_input('u1',2,'不要停止')
        self.assertEqual(self.c.epoch,epoch)
    async def test_name_fast_ack_once(self):
        await self.c.partial_input('u1',1,'鱼宝在吗')
        await self.c.partial_input('u1',2,'鱼宝在吗')
        self.assertEqual(sum(p['type']=='heart-rt-ack' for p in self.packets),1)
    async def test_old_audio_rejected_at_registration_and_receipt(self):
        old=self.c.epoch
        clip=await self.c.register_clip('d1','旧稿。','d1',0,old)
        await self.c.begin('u2')
        self.assertFalse((await self.c.receipt(clip['clip_id'],old,'playing'))['accepted'])
        with self.assertRaises(ValueError):await self.c.register_clip('d1','旧稿。','d1',1,old)
    async def test_queued_audio_does_not_commit_history(self):
        await self.c.register_clip('d1','还没播放的事实。','d1',0,self.c.epoch)
        self.assertFalse(any(k['action']=='commit' for n,k in self.native.calls if n=='heart.realtime'))
    async def test_play_completion_commits_once(self):
        clip=await self.c.register_clip('d1','真正播放完的句子。','d1',0,self.c.epoch)
        for phase in ['playing','completed','completed']:
            await self.c.receipt(clip['clip_id'],self.c.epoch,phase,1)
        calls=[k for n,k in self.native.calls if n=='heart.realtime' and k['action']=='commit']
        self.assertEqual(len(calls),1)
        self.assertEqual(calls[0]['text'],'真正播放完的句子。')
    async def test_interrupted_fragment_never_committed(self):
        clip=await self.c.register_clip('d1','还没有说完','d1',0,self.c.epoch)
        await self.c.receipt(clip['clip_id'],self.c.epoch,'playing',.1)
        await self.c.receipt(clip['clip_id'],self.c.epoch,'stopped',.5)
        self.assertFalse(any(k['action']=='commit' for n,k in self.native.calls if n=='heart.realtime'))
    async def test_invalid_phase_and_nonfinite_receipt_rejected(self):
        clip=await self.c.register_clip('d1','测试。','d1',0,self.c.epoch)
        for phase,pos in [('completed',0),('playing',float('nan')),('playing',-1),('playing',True)]:
            self.assertFalse((await self.c.receipt(clip['clip_id'],self.c.epoch,phase,pos))['accepted'])
    async def test_separate_connections_cannot_use_foreign_clip(self):
        clip=await self.c.register_clip('d1','A的回复。','d1',0,self.c.epoch)
        async def emit(p):pass
        other=engine.Conversation('another-native-session','visitor-b','connection-b',self.native,emit,lambda *a,**k:None)
        await other.begin('u1')
        self.assertFalse((await other.receipt(clip['clip_id'],other.epoch,'playing'))['accepted'])
        await other.close()
    async def test_receipts_out_of_order_do_not_invent_earlier_text(self):
        one=await self.c.register_clip('d1','第一句。','d1',0,self.c.epoch)
        two=await self.c.register_clip('d1','第二句。','d1',1,self.c.epoch)
        await self.c.receipt(two['clip_id'],self.c.epoch,'playing')
        await self.c.receipt(two['clip_id'],self.c.epoch,'completed',1)
        self.assertFalse(any(k['action']=='commit' for n,k in self.native.calls if n=='heart.realtime'))
        await self.c.receipt(one['clip_id'],self.c.epoch,'playing')
        await self.c.receipt(one['clip_id'],self.c.epoch,'completed',1)
        calls=[k for n,k in self.native.calls if n=='heart.realtime' and k['action']=='commit']
        self.assertEqual(calls[-1]['text'],'第一句。第二句。')
    async def test_text_only_commit_with_visible_receipt(self):
        await self.c.begin('u2',voice=False)
        clip=await self.c.register_clip('d1','只显示文字。','d1',0,self.c.epoch)
        await self.c.receipt(clip['clip_id'],self.c.epoch,'visible')
        self.assertTrue(any(k['action']=='commit' for n,k in self.native.calls if n=='heart.realtime'))

    async def test_audio_cannot_commit_from_subtitle_receipt(self):
        clip=await self.c.register_clip('d1','声音尚未播放。','d1',0,self.c.epoch,audio=True)
        self.assertFalse((await self.c.receipt(clip['clip_id'],self.c.epoch,'visible'))['accepted'])
        self.assertFalse(any(k['action']=='commit' for n,k in self.native.calls if n=='heart.realtime'))

    async def test_progress_records_real_seconds_without_committing(self):
        clip=await self.c.register_clip('d1','播放过程中。','d1',0,self.c.epoch)
        await self.c.receipt(clip['clip_id'],self.c.epoch,'playing',0)
        self.assertTrue((await self.c.receipt(clip['clip_id'],self.c.epoch,'progress',.7))['accepted'])
        self.assertFalse((await self.c.receipt(clip['clip_id'],self.c.epoch,'progress',.2))['accepted'])
        self.assertEqual(self.c.clips[clip['clip_id']].position,.7)
        self.assertFalse(any(k['action']=='commit' for n,k in self.native.calls if n=='heart.realtime'))

    async def test_cancelled_prefetch_audit_keeps_old_input_identity(self):
        self.native.read_release=asyncio.Event()
        await self.c.partial_input('u1',1,'查一下会议')
        await asyncio.wait_for(self.native.read_started.wait(),1)
        old_epoch=self.c.epoch
        await self.c.begin('u2')
        await asyncio.sleep(.02)
        rows=[p for feature,p in self.events if feature=='边听边查' and p['phase']=='cancelled']
        self.assertEqual(rows[-1]['utterance_id'],'u1')
        self.assertEqual(rows[-1]['epoch'],old_epoch)
    async def test_native_source_not_last_message(self):
        await self.c.final('u1','输入一。')
        await self.c.native_bound('u1','native-exact-1')
        self.c.log('output','test',output='结果')
        self.assertEqual(self.events[-1][1]['message_id'],'native-exact-1')
        await self.c.begin('u2')
        self.assertEqual(self.events[-1][1]['message_id'],'')
    async def test_read_failure_is_observed(self):
        self.native.fail=True
        await self.c.partial_input('u1',1,'查一下会议')
        await asyncio.sleep(.4)
        self.assertTrue(any(f=='边听边查' and p['phase']=='failed' for f,p in self.events))
        self.assertIsNone(self.c.result)
    async def test_clarification_cooldown(self):
        await self.c.clarify('你指哪次会议？','目标不明')
        await self.c.clarify('你指哪次会议？','目标仍不明')
        self.assertEqual(sum(p['type']=='heart-rt-clarify' for p in self.packets),1)
    async def test_evidence_change_stops_current_sentence_and_requests_correction(self):
        self.c.result=await self.c.read('会议')
        self.c.query='会议'
        self.c.listening=False
        clip=await self.c.register_clip('d1','会议是16:00。','d1',0,self.c.epoch)
        await self.c.receipt(clip['clip_id'],self.c.epoch,'playing',.7)
        self.native.facts={'items':[{'key':'meeting','value':'17:00','version':'2'}]}
        epoch=self.c.epoch
        await self.c.watch_evidence(epoch,'u1','会议')
        self.assertGreater(self.c.epoch,epoch)
        self.assertEqual(self.c.clips[clip['clip_id']].state,'stopped')
        packet=next(p for p in self.packets if p['type']=='heart-rt-correction')
        self.assertEqual(packet['evidence']['completed'],[])
        self.assertEqual(packet['evidence']['interrupted'][0]['seconds'],.7)
        self.assertEqual(packet['evidence']['new'][0]['value'],'17:00')
    async def test_changed_result_not_in_speech_is_not_correction(self):
        self.c.result=await self.c.read('会议')
        self.c.listening=False
        clip=await self.c.register_clip('d1','先聊一下别的。','d1',0,self.c.epoch)
        await self.c.receipt(clip['clip_id'],self.c.epoch,'playing')
        self.native.facts={'items':[{'key':'meeting','value':'17:00','version':'2'}]}
        task=asyncio.create_task(self.c.watch_evidence(self.c.epoch,'u1','会议'))
        await asyncio.sleep(2.15)
        task.cancel()
        await asyncio.gather(task,return_exceptions=True)
        self.assertFalse(any(p['type']=='heart-rt-correction' for p in self.packets))


if __name__=='__main__':unittest.main(verbosity=2)
