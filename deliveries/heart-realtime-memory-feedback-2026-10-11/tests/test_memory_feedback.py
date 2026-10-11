from pathlib import Path
import sys, unittest, asyncio, time
from types import SimpleNamespace
from unittest.mock import patch, AsyncMock
W=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(W/'maibot'));sys.path.insert(0,str(W/'tests'))
from support import engine,drafts,host,Model,Generator,Item,native_modules
from heart_shared.realtime_evidence import evidence_note,evidence_summary,memory_status
from heart_shared.conversation_view import render_input
from route2plugin.speculation import eligibility_reason,evidence_key
EMPTY={'native':{'success':True,'items':[]},'local':{'items':[]},'weather':{'success':True,'content':'下午降雨'},'request':{'memory':'制作机器人','weather':{'city':'西安市长安区'}}}

class Evidence(unittest.TestCase):
    def test_empty_requires_disclosure_and_both_questions(self):
        note=evidence_note(EMPTY)
        self.assertIn('回复必须明确说明',note);self.assertIn('两项都要回应',note)
        self.assertIn('刚写入的记忆不算',note);self.assertEqual(memory_status(EMPTY),('no_hits',0))
    def test_failure_not_empty(self):
        failed={**EMPTY,'native':{'success':False,'items':[],'error':'timeout'}}
        self.assertEqual(memory_status(failed)[0],'unverified');self.assertIn('查询失败不同于无命中',evidence_note(failed))
        self.assertNotEqual(evidence_key(EMPTY),evidence_key(failed))
    def test_hits_not_false_negative(self):
        found={**EMPTY,'native':{'items':[{'content':'我做机器人','hash':'one'}]}}
        self.assertEqual(memory_status(found),('hits',1));self.assertNotIn('回复必须明确说明',evidence_note(found))
    def test_weather_only_not_memory_miss(self):
        weather={'weather':EMPTY['weather']}
        self.assertEqual(memory_status(weather)[0],'not_requested');self.assertNotIn('回复必须明确说明',evidence_note(weather))
    def test_explicit_decision_reasons(self):
        for text,code in [('短','too_short'),('a'*241,'too_long'),('/search 你好','command'),('我想告诉你但是','incomplete'),('请提醒我明天带伞','confirmation_required')]:
            self.assertEqual(eligibility_reason(text)[0],code)
        self.assertIn('提醒',eligibility_reason('请提醒我明天带伞')[1])

class PromptPath(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.model=Model();self.generator=Generator(self.model);self.events=[]
        self.runtime=SimpleNamespace(_chat_history=[])
        self.lease=host.Lease('visitor','connection',epoch=1,active=True,preemptive=True,utterance='u1',native_id='formal1')
        self.lease.evidence=EMPTY;self.lease.evidence_key=evidence_key(EMPTY)
        host.LEASES['session']=self.lease
        self.p=patch.dict('sys.modules',native_modules(self.runtime,self.generator));self.p.start()
        self.a=patch.object(drafts,'audit',lambda s,l,p,**d:self.events.append((p,d)));self.a.start()
    async def asyncTearDown(self):
        await drafts.cancel_prepared('session',self.lease,'test done');self.a.stop();self.p.stop();host.LEASES.clear()
    async def generate(self):
        async def factory(*args):return [Item('原生回复提示')]
        return await drafts.generate_reply(session_id='session',generator=self.generator,model=self.model,context_factory=factory,
            options=None,reply_message=SimpleNamespace(message_id='formal1',processed_plain_text='你还记得机器人吗'),
            requirements='',tool_args={},task_name='replyer',model_name=None,retry_count=0)
    async def test_formal_model_receives_empty_and_weather_and_audit(self):
        await self.generate();note=self.model.calls[0][-1]
        self.assertIn('回复必须明确说明',note);self.assertIn('下午降雨',note)
        event=next(d for p,d in self.events if p=='context_built')
        self.assertEqual(event['generation_mode'],'正式回复');self.assertIn('无命中',event['summary'])
    async def test_speculative_model_uses_identical_disclosure(self):
        await drafts.prepare(SimpleNamespace(session_id='session'),self.lease,{'epoch':1,'utterance_id':'u1','text':'你还记得机器人吗','evidence':EMPTY,'evidence_key':self.lease.evidence_key})
        self.assertIn('回复必须明确说明',self.model.calls[0][-1])
        self.assertTrue(any(d.get('generation_mode')=='提前草稿' for p,d in self.events))
    async def test_late_error_regenerates_with_correct_status(self):
        self.model.block_first=True;job=asyncio.create_task(self.generate())
        for _ in range(100):
            if self.model.calls:break
            await asyncio.sleep(.01)
        event=self.lease.evidence_changed
        self.lease.evidence={**EMPTY,'native':{'success':False,'items':[],'error':'timeout'}}
        self.lease.evidence_key=evidence_key(self.lease.evidence);self.lease.evidence_changed=asyncio.Event();event.set()
        await asyncio.wait_for(job,2)
        self.assertEqual(len(self.model.calls),2);self.assertIn('查询失败不同于无命中',self.model.calls[-1][-1])

class Decision(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.events=[];self.calls=[]
        async def call(name,**kw):
            self.calls.append((name,kw));return {'success':True,'local_cancelled':True,'items':[]}
        self.c=engine.Conversation('session','visitor','connection',call,AsyncMock(),lambda f,s,**d:self.events.append((f,d)))
        self.c.stable_seconds=.02;await self.c.begin('u1',audio=True);await self.c.set_mode(True)
    async def asyncTearDown(self):await self.c.close()
    async def test_reminder_reason_no_model_and_deduplicated(self):
        text='如果下午下雨请提醒我带伞'
        await self.c.partial_input('u1',1,text);await self.c.partial_input('u1',2,text+'。')
        skips=[d for f,d in self.events if d.get('phase')=='skipped']
        self.assertEqual(len(skips),1);self.assertEqual(skips[0]['decision_code'],'confirmation_required')
        await self.c.input_end('u1');await self.c.final('u1',text)
        self.assertFalse(any(d.get('action')=='speculate' for n,d in self.calls))
        final=[d for f,d in self.events if d.get('checkpoint')=='final'][0]
        self.assertIn('提醒',final['reason']);self.assertIn('程序规则',final['decision_by'])
    async def test_gate_disabled_pending_time_limit(self):
        self.c.partial='今天可以讲一下吗';self.c.preemptive=False
        self.assertEqual(self.c.speculation_reason()[0],'disabled')
        self.c.preemptive=True;self.c.audio_input=False
        self.assertEqual(self.c.speculation_reason()[0],'text_input')
        self.c.audio_input=True;self.c.query='机器人'
        self.assertEqual(self.c.speculation_reason()[0],'query_pending')
        self.c.result=EMPTY;self.c.started=time.monotonic()-21
        self.assertEqual(self.c.speculation_reason()[0],'time_limit')
    async def test_recording_end_no_stable_pause_is_explicit(self):
        await self.c.partial_input('u1',1,'今天过得怎么样呢');await self.c.input_end('u1')
        self.assertTrue(any(d.get('decision_code')=='not_stable' for f,d in self.events))
    async def test_read_miss_metadata_and_context(self):
        result=await self.c.read('我之前说在做机器人')
        self.assertIn('memory',result['request']);self.c.result=result;self.c.query=self.c.intent('你还记得我之前说在做机器人吗')
        await self.c.final('u1','你还记得我之前说在做机器人吗')
        self.assertTrue(any('回复必须明确说明' in d.get('text','') for n,d in self.calls if d.get('action')=='context'))

class Rendering(unittest.TestCase):
    def test_human_log_shows_gate_reason_and_prompt_handoff(self):
        def event(data):return {'seq':1,'kind':'实时对话插件','time':'2026-10-11T12:27:50+08:00','data':data}
        events=[event({'feature':'流式听取','phase':'started','audio_input':True}),
                event({'feature':'提前生成','phase':'skipped','reason':'命中“提醒”，等待正式转写','decision_code':'confirmation_required','decision_by':'程序规则'}),
                event({'feature':'回复查询依据','phase':'context_built','generation_mode':'正式回复','summary':evidence_summary(EMPTY)})]
        text=render_input({'key':('utterance','u1'),'events':events,'ids':[]}, {}, 1)
        self.assertIn('命中“提醒”',text);self.assertIn('判断来源：程序规则',text);self.assertIn('回复模型查询依据',text);self.assertIn('无命中',text)

if __name__=='__main__':unittest.main(verbosity=2)
