from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace, ModuleType
from unittest.mock import AsyncMock, patch
import asyncio
import importlib.util
import sys
import unittest

from heart_shared.conversation_view import render_conversation, SEPARATOR
from heart_shared.console_view import console_summary
from heart_shared.input_groups import group_inputs
from support import engine
Conversation=engine.Conversation

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('compact_adapter',ROOT/'vt/heart_realtime_client.py')
adapter=importlib.util.module_from_spec(spec);spec.loader.exec_module(adapter)


def e(seq, kind='实时对话插件', **data):
    return dict(seq=seq, time=f'2026-10-10T20:00:{seq:02d}.000+08:00',
                day='2026-10-10',session='s',kind=kind,data=data)


class ViewTests(unittest.TestCase):
    def test_console_omits_partial_and_unchanged_polling(self):
        self.assertIsNone(console_summary('流式听取', {'phase':'partial','text':'你'}))
        self.assertIsNone(console_summary('只读工具调用', {'phase':'completed','purpose':'回复期间复查证据'}))

    def test_console_keeps_results_and_failures_concise(self):
        self.assertIn('无命中',console_summary('只读工具调用', {'phase':'completed','tool':'read','output':{'items':[]}}))
        self.assertIn('ConnectionError',console_summary('故障隔离', {'phase':'failed','reason':'ConnectionError'}))
        self.assertNotIn('secret',console_summary('流式听取', {'phase':'started','output':'开始','connection_id':'secret'}))

    def test_query_stays_stable_when_user_continues_another_clause(self):
        self.assertEqual(Conversation.intent('帮我查一下考试时间我还想知道准备哪些东西'),'考试时间')
        self.assertEqual(Conversation.intent('帮我查一下'),'')
        self.assertEqual(Conversation.intent('帮我查一下考'),'')
        self.assertEqual(Conversation.intent('帮我查一下考试时间我还'),'考试时间')
        self.assertEqual(Conversation.intent('帮我查一下考试时间，顺便想问天气'),'考试时间')

    def test_one_full_width_separator_per_voice_input(self):
        events=[e(0,utterance_id='a',feature='流式听取',phase='started',audio_input=True),
                e(1,utterance_id='a',feature='流式听取',phase='partial',text='你'),
                e(2,utterance_id='a',feature='流式听取',phase='partial',text='你好呀'),
                e(3,utterance_id='a',message_id='m',feature='正式输入',phase='native_bound'),
                e(4,'心情变化',message_id='m',mood={'before':50,'value':53}),
                e(5,'生成回复（尚非送达）',message_id='m',response='你好'),
                e(6,utterance_id='b',feature='流式听取',phase='started',audio_input=True)]
        text=render_conversation('2026-10-10',events,{})
        self.assertEqual(text.splitlines().count(SEPARATOR),2)
        self.assertEqual(len(SEPARATOR),72)
        block=text.split(SEPARATOR)[1]
        self.assertIn('心情：50 → 53',block)
        self.assertIn('正式回复生成完成：你好',block)
        self.assertNotIn('转写文字：你',text)

    def test_failed_asr_task_correlates_without_native_message(self):
        events=[e(0,utterance_id='u',feature='流式听取',phase='started',audio_input=True),
                e(1,utterance_id='u',task_id='t',feature='输入关联',phase='bound'),
                e(2,'语音识别',task_id='t',text='用户原话',phase='asr_completed'),
                e(3,'Live2D交互',task_id='t',phase='failed',status='连接中断')]
        groups=group_inputs(events,{},lambda data:[data['message_id']] if data.get('message_id') else [])
        self.assertEqual(len(groups),1)
        text=render_conversation('2026-10-10',events,{})
        self.assertIn('用户原话',text);self.assertIn('连接中断',text)

    def test_control_background_has_no_input_separators(self):
        text=render_conversation('2026-10-10',[e(0,feature='插件开关',phase='changed'),
                  e(1,feature='会话绑定',phase='completed')],{})
        self.assertNotIn(SEPARATOR,text)
        self.assertIn('后台记录已合并',text)

    def test_tool_output_retained_while_progress_hidden(self):
        events=[e(0,utterance_id='u',feature='流式听取',phase='started',audio_input=True),
                e(1,utterance_id='u',tool='local.facts',phase='started',listening=True,input='考试'),
                e(2,utterance_id='u',tool='local.facts',phase='completed',output={'items':[{'value':'明天下午七点'}]}),
                e(3,utterance_id='u',feature='输出与播放账本',phase='progress',position_seconds=1)]
        text=render_conversation('2026-10-10',events,{})
        self.assertIn('录音未结束',text);self.assertIn('明天下午七点',text)
        self.assertNotIn('position_seconds',text)

    def test_draft_is_not_counted_twice(self):
        events=[e(0,utterance_id='u',feature='流式听取',phase='started',audio_input=True),
                e(1,utterance_id='u',feature='提前生成',phase='started',temporary=True,input='问题'),
                e(2,utterance_id='u',feature='提前生成',phase='started',temporary=True,input='问题',model_task='replyer'),
                e(3,utterance_id='u',feature='提前生成',phase='ready',output='隐藏草稿'),
                e(4,utterance_id='u',feature='流式听取',phase='ended'),
                e(5,utterance_id='u',feature='正式输入',phase='accepted',text='问题')]
        text=render_conversation('2026-10-10',events,{})
        self.assertEqual(text.count('提前生成开始'),1)
        self.assertIn('录音结束前',text);self.assertIn('隐藏草稿',text)

    def test_missing_end_never_claims_before_speech_end(self):
        events=[e(0,utterance_id='u',feature='提前生成',phase='started',temporary=True),
                e(1,utterance_id='u',feature='正式输入',phase='accepted',text='问题')]
        text=render_conversation('2026-10-10',events,{})
        self.assertIn('未采集录音结束',text)
        self.assertNotIn('提前生成开始 [录音结束前]',text)


class EngineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.records=[];self.calls=[]
        async def call(tool,**args):
            self.calls.append(args)
            return {'success':True,'local_cancelled':True,'items':[],'output':'隐藏草稿'}
        async def emit(packet):pass
        self.c=Conversation('s','v','c',call,emit,lambda feature,session,**data:self.records.append(data))
        self.c.stable_seconds=.06
        await self.c.begin('u',audio=True)
        await self.c.set_mode(True)
    async def asyncTearDown(self):await self.c.close()

    async def test_identical_asr_revision_does_not_postpone_generation(self):
        await self.c.partial_input('u',1,'今天过得怎么样呢')
        await asyncio.sleep(.04)
        await self.c.partial_input('u',2,'今天过得怎么样呢')
        await asyncio.sleep(.035)
        self.assertEqual(sum(a.get('action')=='speculate' for a in self.calls),1)
        await self.c.input_end('u')

    async def test_no_speculation_can_start_after_recording_end(self):
        await self.c.partial_input('u',1,'今天过得怎么样呢')
        await self.c.input_end('u')
        await asyncio.sleep(.09)
        self.assertFalse(any(a.get('action')=='speculate' for a in self.calls))
        await self.c.final('u','今天过得怎么样呢')
        self.assertTrue(any(e.get('phase')=='skipped' and '稳定' in e.get('reason','') for e in self.records))

    async def test_old_end_cannot_end_new_input(self):
        await self.c.begin('new',audio=True)
        with self.assertRaises(ValueError):await self.c.input_end('u')
        self.assertTrue(self.c.listening)

    async def test_queries_have_real_starts_before_end(self):
        await self.c.set_mode(False)
        await self.c.partial_input('u',1,'帮我查询一下考试时间')
        await asyncio.sleep(.3)
        self.assertTrue(any(e.get('tool') and e['phase']=='started' and e['listening'] for e in self.records))
        await self.c.input_end('u')


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_next_begin_rebinds_dead_link_and_preserves_switches(self):
        web=SimpleNamespace(send_text=AsyncMock())
        handler=SimpleNamespace(client_contexts={'v':object()},client_connections={'v':web},
            chat_group_manager=SimpleNamespace(get_client_group=lambda uid:None))
        dead=SimpleNamespace(alive=False,close=AsyncMock())
        adapter.LINKS['v']=dead;adapter.MODES['v']={'enabled':True,'preemptive':True}
        class NewLink:
            def __init__(self,*args):
                self.alive=True;self.connection='new';self.needs_fence_reset=True
                self.ack_audio=None;self.ack_task=None;self.calls=[]
            async def open(self):pass
            async def request(self,action,**kwargs):
                self.calls.append((action,kwargs));return {'epoch':1}
        with patch.object(adapter,'Link',NewLink):
            await adapter.event(handler,web,'v',{'action':'begin','utterance_id':'u','audio':True,'voice':False})
        link=adapter.LINKS['v']
        dead.close.assert_awaited_once()
        self.assertEqual(link.calls[0],('mode',{'preemptive':True}))
        self.assertIn('"new_connection": true',web.send_text.call_args.args[0])

    async def test_audio_after_disconnect_never_replayed_to_new_session(self):
        web=SimpleNamespace(send_text=AsyncMock())
        handler=SimpleNamespace(client_contexts={'v':object()},client_connections={'v':web},
            chat_group_manager=SimpleNamespace(get_client_group=lambda uid:None))
        with patch.object(adapter,'Link') as create:
            await adapter.event(handler,web,'v',{'action':'audio','utterance_id':'old','samples':[0]})
            create.assert_not_called()

    async def asyncSetUp(self):
        adapter.LINKS.clear();adapter.MODES.clear()
        self.trace=SimpleNamespace(stage=lambda *args,**kw:None)
        fake=ModuleType('dialogue_trace');fake.current_turn=SimpleNamespace(get=lambda:self.trace)
        self.modules=patch.dict(sys.modules,{'dialogue_trace':fake});self.modules.start()
    async def asyncTearDown(self):
        self.modules.stop();adapter.LINKS.clear();adapter.MODES.clear()

    async def test_stale_link_does_not_drop_complete_input(self):
        link=SimpleNamespace(alive=False,listening=False,request=AsyncMock())
        adapter.LINKS['v']=link
        self.assertIsNone(await adapter.final_input('v','完整原话',{'heart_rt_utterance':'u'}))
        link.request.assert_not_awaited()

    async def test_real_host_error_is_not_disguised_as_fallback(self):
        link=SimpleNamespace(alive=True,listening=False,request=AsyncMock(side_effect=RuntimeError('身份不匹配')))
        adapter.LINKS['v']=link
        with self.assertRaises(RuntimeError):await adapter.final_input('v','原话',{'heart_rt_utterance':'u'})

    async def test_transport_reset_falls_back(self):
        link=SimpleNamespace(alive=True,listening=False,request=AsyncMock(side_effect=ConnectionResetError('断开')))
        adapter.LINKS['v']=link
        self.assertIsNone(await adapter.final_input('v','原话',{'heart_rt_utterance':'u'}))

    async def test_link_alive_requires_running_reader(self):
        link=adapter.Link('v',None,None)
        link.socket=SimpleNamespace(closed=False)
        link.reader=SimpleNamespace(done=lambda:True)
        self.assertFalse(link.alive)


if __name__=='__main__':unittest.main(verbosity=2)
