from pathlib import Path
from types import SimpleNamespace, ModuleType
from unittest.mock import patch

import asyncio
import importlib.util
import sys
import unittest

ROOT=Path(__file__).resolve().parents[1]
from support import drafts
spec=importlib.util.spec_from_file_location('rt_host',ROOT/'maibot/src/services/heart_realtime.py')
host=importlib.util.module_from_spec(spec)
sys.modules[spec.name]=host
spec.loader.exec_module(host)
VISITOR='aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'
SESSION='native-registered-test-session'


class HostTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        host.LEASES.clear()
        self.session=SimpleNamespace(platform='webui',is_group_session=False,
            user_id='webui_user_vtuber_visit_'+VISITOR.replace('-',''),session_id=SESSION)
        self.sessions={SESSION:self.session}
        self.runtime=SimpleNamespace(_internal_loop_task=None,_internal_turn_queue=asyncio.Queue(),
                                     _ensure_background_tasks_running=lambda:None)
        self.manager=SimpleNamespace(heartflow_chat_list={SESSION:self.runtime})
        self.saved=[]
        self.automation=[]
        self.history=[]
        async def store(msg):self.saved.append(msg)
        async def auto(msg):self.automation.append(msg)
        def sync(msg,source_kind):self.history.append(msg)
        def module(name,**attrs):
            m=ModuleType(name)
            m.__dict__.update(attrs)
            return m
        self.modules={
            'src.config.config':module('config',global_config=SimpleNamespace(
                bot=SimpleNamespace(nickname='测试角色',alias_names=['测试别名']))),
            'src.services.heart_realtime_drafts':drafts,
            'src.chat.message_receive.chat_manager':module('chat_manager',chat_manager=SimpleNamespace(
                get_existing_session_by_session_id=lambda sid:self.sessions.get(sid))),
            'src.chat.heart_flow.heartflow_manager':module('heartflow_manager',heartflow_manager=self.manager),
            'src.plugin_runtime.host.message_utils':module('message_utils',PluginMessageUtils=SimpleNamespace(
                _message_sequence_from_dict=lambda segments:segments,
                _message_sequence_to_dict=lambda segments:segments)),
            'src.services.send_service':module('send_service',_store_sent_message=store,
                _notify_memory_automation_on_message_sent=auto,_sync_sent_message_to_maisaka_history=sync),
        }
        self.patch=patch.dict(sys.modules,self.modules)
        self.patch.start()
        await self.call('bind')
        await self.call('begin',epoch=1)
        await self.call('activate',epoch=1)
    async def asyncTearDown(self):
        self.patch.stop()
        host.LEASES.clear()
    async def call(self,action,**args):
        return await host.capability('heart.realtime','heart.realtime',dict(action=action,
            session_id=SESSION,visitor_id=VISITOR,connection_id='connection-a',**args))
    def message(self,text='第一句。第二句。'):
        return SimpleNamespace(platform='webui',session_id=SESSION,message_id='native-draft-1',
            processed_plain_text=text,display_message=text,raw_message=[{'type':'text','data':text}])
    async def test_only_granted_plugin(self):
        with self.assertRaises(PermissionError):await host.capability('different.plugin','heart.realtime',{
            'action':'bind','session_id':SESSION,'visitor_id':VISITOR,'connection_id':'connection-a'})
    async def test_binding_reports_api_and_current_character(self):
        result=await self.call('bind')
        self.assertEqual(result['api_version'],1)
        self.assertEqual(result['character_names'],['测试角色','测试别名'])
    async def test_qq_never_managed(self):
        m=SimpleNamespace(platform='qq',session_id=SESSION,message_id='qq-1')
        self.assertFalse(host.is_managed(m))
        self.assertTrue(host.can_deliver(m))
        self.assertFalse(host.defer_delivery(m))
        self.session.platform='qq'
        with self.assertRaises(PermissionError):await self.call('heartbeat')
    async def test_group_session_never_bound(self):
        self.session.is_group_session=True
        with self.assertRaises(PermissionError):await self.call('heartbeat')
    async def test_ordinary_webui_unchanged(self):
        m=SimpleNamespace(platform='webui',session_id='ordinary-native-session')
        self.assertTrue(host.can_deliver(m))
        self.assertFalse(host.defer_delivery(m))
    async def test_foreign_visitor_rejected(self):
        with self.assertRaises(PermissionError):await host.capability('heart.realtime','heart.realtime',{
            'action':'heartbeat','session_id':SESSION,'visitor_id':'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb',
            'connection_id':'connection-a'})
    async def test_foreign_connection_rejected(self):
        with self.assertRaises(PermissionError):await host.capability('heart.realtime','heart.realtime',{
            'action':'heartbeat','session_id':SESSION,'visitor_id':VISITOR,'connection_id':'connection-b'})
    async def test_unregistered_session_rejected(self):
        self.sessions.clear()
        with self.assertRaises(PermissionError):await self.call('heartbeat')
    async def test_draft_transport_has_no_formal_commit(self):
        self.assertTrue(host.defer_delivery(self.message()))
        self.assertEqual(self.saved,[])
        self.assertEqual(self.history,[])
        self.assertEqual(self.automation,[])
    async def test_completed_prefix_commits_without_unheard_tail(self):
        host.defer_delivery(self.message())
        await self.call('commit',draft_id='native-draft-1',epoch=1,text='第一句。')
        self.assertEqual(self.saved[-1].processed_plain_text,'第一句。')
        self.assertEqual(self.history[-1].processed_plain_text,'第一句。')
        self.assertEqual(self.automation[-1].processed_plain_text,'第一句。')
        self.assertTrue(self.saved[-1].message_id.startswith('rt-heard-'))
        self.assertEqual(self.saved[-1].raw_message,[{'type':'text','data':'第一句。'}])
    async def test_repeat_receipt_does_not_duplicate_memory(self):
        host.defer_delivery(self.message())
        for _ in range(3):await self.call('commit',draft_id='native-draft-1',epoch=1,text='第一句。')
        self.assertEqual(len(self.saved),1)
        self.assertEqual(len(self.automation),1)
    async def test_further_prefix_only_commits_delta(self):
        host.defer_delivery(self.message())
        await self.call('commit',draft_id='native-draft-1',epoch=1,text='第一句。')
        await self.call('commit',draft_id='native-draft-1',epoch=1,text='第一句。第二句。')
        self.assertEqual([m.processed_plain_text for m in self.saved],['第一句。','第二句。'])
        self.assertNotEqual(self.saved[0].message_id,self.saved[1].message_id)
    async def test_unfinished_fragment_rejected(self):
        host.defer_delivery(self.message())
        result=await self.call('commit',draft_id='native-draft-1',epoch=1,text='第一')
        self.assertTrue(result['pending'])
        self.assertEqual(self.saved,[])
    async def test_forged_output_rejected(self):
        host.defer_delivery(self.message())
        with self.assertRaises(ValueError):await self.call('commit',draft_id='native-draft-1',epoch=1,text='未生成的事实。')
    async def test_stop_blocks_delivery_and_old_commit(self):
        host.defer_delivery(self.message())
        await self.call('begin',epoch=2)
        self.assertFalse(host.can_deliver(self.message()))
        with self.assertRaises(ValueError):await self.call('commit',draft_id='native-draft-1',epoch=1,text='第一句。')
    async def test_close_restores_normal_webui_path(self):
        await self.call('close')
        self.assertFalse(host.is_managed(self.message()))
        self.assertTrue(host.can_deliver(self.message()))
    async def test_busy_loop_really_cancelled(self):
        began=asyncio.Event()
        async def loop():
            began.set()
            await asyncio.Future()
        task=asyncio.create_task(loop())
        self.runtime._internal_loop_task=task
        await began.wait()
        result=await self.call('begin',epoch=2)
        self.assertTrue(task.cancelled())
        self.assertTrue(result['local_cancelled'])
    async def test_evidence_marks_generation_finished_before_transport_as_stale(self):
        host.LEASES[SESSION].reply_evidence_key=''
        result=await self.call('evidence',epoch=1,result={'items':['新材料']},evidence_key='new')
        self.assertTrue(result['reply_ready_stale'])
        await self.call('begin',epoch=2)
        self.assertIsNone(host.LEASES[SESSION].reply_evidence_key)
    async def test_cancel_does_not_touch_another_runtime(self):
        async def loop():await asyncio.Future()
        other=asyncio.create_task(loop())
        self.manager.heartflow_chat_list['native-qq-group']=SimpleNamespace(_internal_loop_task=other)
        await self.call('begin',epoch=2)
        self.assertFalse(other.cancelled())
        other.cancel()
        await asyncio.gather(other,return_exceptions=True)

    async def test_cancel_pending_blocks_new_output(self):
        host.LEASES[SESSION].cancel_pending=True
        host.LEASES[SESSION].active=False
        with self.assertRaises(RuntimeError):await self.call('activate',epoch=1)
        self.assertFalse(host.can_deliver(self.message()))

    async def test_quoted_question_is_not_spoken_body(self):
        m=self.message('问句 回复正文。')
        m.raw_message=[{'type':'reply','data':'问句'},{'type':'text','data':'回复正文。'}]
        host.defer_delivery(m)
        found=await self.call('draft',text='回复正文。')
        self.assertEqual(found['text'],'回复正文。')
        await self.call('commit',draft_id=found['draft_id'],epoch=1,text='回复正文。')
        self.assertEqual(self.saved[0].processed_plain_text,'回复正文。')

    async def test_draft_not_yet_registered_is_pending_not_failure(self):
        result=await self.call('draft',text='尚未登记。')
        self.assertTrue(result['success'])
        self.assertTrue(result['pending'])

    async def test_same_text_two_native_drafts_reserved_separately(self):
        first=self.message('我在。');second=self.message('我在。');second.message_id='native-draft-2'
        host.defer_delivery(first);host.defer_delivery(second)
        a=await self.call('draft',text='我在。',delivery_key='a'*32)
        b=await self.call('draft',text='我在。',delivery_key='b'*32)
        self.assertNotEqual(a['draft_id'],b['draft_id'])
        retry=await self.call('draft',text='我在。',delivery_key='a'*32)
        self.assertEqual(retry['draft_id'],a['draft_id'])


if __name__=='__main__':unittest.main(verbosity=2)
