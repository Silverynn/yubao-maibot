"""无模型费用、无QQ消息：两层状态、生命周期、持久化和日志归属回归。"""
import asyncio
import json
import logging
import sys
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from test_heart_mood import ROOT, MoodEngine, plugin
from test_mood_plugin.appraiser import AIAppraiser, parse_assessment
from heart_shared.storage import AuditStore

sys.path.insert(0, str(ROOT / 'vtuber'))
from heart_bridge import HeartBridge, AvatarReceipt


class TopicTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2030, 1, 1, tzinfo=timezone.utc)
        self.store = AuditStore(ROOT / '.runtime/test-data' / uuid.uuid4().hex, clock=lambda: self.now)
        self.engine = MoodEngine(self.store)
        self.config = plugin.Config(emotions=plugin.TopicEmotions(enabled=True),
                                    mood=plugin.Settings(baseline=80, recovery_per_hour=0))

    def message(self, mid='m1', session='live', text='这个证明看不懂', platform='webui', user='webui_user_vtuber_local_user'):
        return {'session_id':session,'message_id':mid,'processed_plain_text':text,
                'message_info':{'platform':platform,'user_info':{'user_id':user,'user_nickname':'测试访客'}}}

    def assessment(self, action='set', label='疑惑', topic='数学证明', reason='对证明步骤感到疑惑'):
        return {'method':'AI判断','delta':0,'reason':'长期心情不变','emotion':{
            'action':action,'label':label,'topic':topic,'intensity':.6,
            'reason':reason,'confidence':.9,'evidence':'看不懂'}}

    def apply(self, mid='m1', decision=None, **kwargs):
        return self.engine.update(self.message(mid=mid, **kwargs), self.config.mood,
                                 self.config.rules, decision or self.assessment(), self.config)

    def current(self, session='live'):
        with self.store.connect() as db:
            return self.store.emotion_snapshot(db,session)

    def kinds(self):
        with self.store.connect() as db:
            return [row[0] for row in db.execute('SELECT kind FROM events ORDER BY seq')]

    def test_high_mood_confusion_does_not_expire_or_cross_sessions(self):
        state = self.apply()
        self.assertEqual(state['value'],80)
        self.assertEqual(self.current()['label'],'疑惑')
        self.now += timedelta(days=1)
        self.apply('m2', self.assessment(action='keep',reason='仍在讨论证明'))
        with self.store.connect() as db:
            self.assertTrue(AuditStore(self.store.root).emotion_snapshot(db,'live')['active'])
        self.assertFalse(self.current('qq').get('active'))
        bridge=HeartBridge(self.store,['平静','问号','星星眼'])
        display=bridge.state()
        self.assertEqual((display['value'],display['label'],display['expression']),(80,'疑惑','问号'))
        self.assertEqual(bridge.state()['command_id'],display['command_id'])

    def test_current_model_catalog_drives_expression_and_switch(self):
        self.apply()
        model = SimpleNamespace(live2d_model_name='mao_pro',
                                emo_map={'neutral': 0, 'joy': 3, 'anger': 2})
        bridge = HeartBridge(self.store, model_provider=lambda: model)
        first = bridge.state()
        self.assertEqual((first['model_name'], first['expression'], first['label']),
                         ('mao_pro', 0, '疑惑'))
        self.assertTrue(first['valid_expression'])
        self.assertEqual(bridge.expressions()['expressions']['neutral'], 0)
        bridge.manual(0)
        self.assertEqual(bridge.state()['expression'], 0)
        bridge.manual(None)
        previous_command = bridge.state()['command_id']
        model.live2d_model_name = 'another-model'
        model.emo_map = {'neutral': '待机', 'confusion': '歪头'}
        switched = bridge.state()
        self.assertEqual(switched['expression'], '歪头')
        self.assertNotEqual(switched['command_id'], previous_command)
        with self.assertRaises(ValueError):
            bridge.manual('问号')

    def test_model_file_expressions_are_listed_beyond_emotion_map(self):
        self.apply()
        model = SimpleNamespace(live2d_model_name='ds-whale-girl',
            model_info={'url':'/live2d-models/ds-whale-girl/c_0120.model3.json'},
            emo_map={'neutral':'平静'})
        bridge = HeartBridge(self.store, model_provider=lambda:model)
        self.assertIn('情绪花花', bridge.expressions()['model_expression_names'])
        bridge.manual('情绪花花')
        self.assertEqual(bridge.state()['expression'], '情绪花花')

    def test_explicit_end_restores_current_mood_base_and_logs(self):
        self.apply()
        self.apply('m2',self.assessment(action='clear',reason='用户表示已经解决'),text='已经解决啦')
        self.assertFalse(self.current()['active'])
        self.assertIn('临时情绪结束',self.kinds())
        display=HeartBridge(self.store,['问号','星星眼']).state()
        self.assertEqual(display['expression'],'星星眼')
        self.assertEqual(display['value'],80)
        text=next((self.store.root/'logs/Live2D').glob('*.txt')).read_text(encoding='utf-8-sig')
        self.assertIn('用户表示已经解决',text)
        self.assertIn('触发对话：已经解决啦',text)

    def test_topic_replacement_ends_old_before_starting_new(self):
        self.apply()
        self.apply('m2',self.assessment(label='开心',topic='比赛获奖',reason='新的喜事'))
        self.assertEqual([k for k in self.kinds() if k in {'临时情绪产生','临时情绪结束'}],
                         ['临时情绪产生','临时情绪结束','临时情绪产生'])

    def test_failure_keep_and_duplicate(self):
        self.apply()
        revision=self.current()['revision']
        self.apply('m2',{'method':'AI未完成','delta':0,'reason':'超时'})
        self.assertEqual(self.current()['revision'],revision)
        count=len(self.kinds())
        self.apply('m2')
        self.assertEqual(len(self.kinds()),count)

    def test_parser_rejects_bad_emotion_without_losing_valid_mood(self):
        raw={'delta':0,'confidence':.9,'reason':'中性','evidence':'','emotion':self.assessment()['emotion']}
        parsed=lambda:parse_assessment(json.dumps(raw,ensure_ascii=False),'看不懂',.7,self.config.emotions)
        self.assertEqual(parsed()['emotion']['label'],'疑惑')
        for key,value in [('confidence',.2),('intensity',float('nan')),('label','未知类'),('evidence','不存在')]:
            old=raw['emotion'][key];raw['emotion'][key]=value
            result=parsed()
            self.assertIn('emotion_error',result)
            self.assertNotIn('emotion',result)
            self.assertEqual(result['delta'],0)
            raw['emotion'][key]=old

    def test_qq_and_live2d_folders_and_no_private_state_in_bridge(self):
        self.apply()
        self.apply('q1',self.assessment(label='生气',topic='QQ私事'),session='qq',platform='qq',user='someone')
        self.assertEqual(HeartBridge(self.store,['问号']).state()['label'],'疑惑')
        qq=next((self.store.root/'logs/QQ').glob('*.txt')).read_text(encoding='utf-8-sig')
        live=next((self.store.root/'logs/Live2D').glob('*.txt')).read_text(encoding='utf-8-sig')
        self.assertIn('QQ私事',qq)
        self.assertNotIn('QQ私事',live)

    def test_receipt_is_deduplicated_and_uses_dispatched_state(self):
        self.apply()
        bridge=HeartBridge(self.store,['问号','星星眼'])
        state=bridge.state()
        self.apply('m2',self.assessment(action='clear'))
        report=AvatarReceipt(command_id=state['command_id'],client_id='browser',status='applied')
        bridge.receipt(report);bridge.receipt(report)
        with self.store.connect() as db:
            rows=[json.loads(r[0]) for r in db.execute("SELECT payload FROM events WHERE kind='Live2D动作'")]
        self.assertEqual(len(rows),2)
        self.assertEqual(rows[-1]['expression'],'问号')
        self.assertEqual(rows[-1]['emotion']['label'],'疑惑')

    def test_manual_temporarily_overrides_without_changing_emotion(self):
        self.apply()
        now=[100.0]
        bridge=HeartBridge(self.store,['问号','星星眼','生气','开心兴奋'],clock=lambda:now[0])
        original=self.current()
        bridge.manual('开心兴奋')
        manual=bridge.state()
        self.assertTrue(manual['manual'])
        self.assertEqual(manual['expression'],'开心兴奋')
        self.assertEqual(manual['label'],'疑惑')
        self.assertEqual(self.current(),original)
        self.apply('new',self.assessment(label='生气',topic='另一个话题'))
        self.assertEqual(bridge.state()['command_id'],manual['command_id'])
        now[0]+=9
        restored=bridge.state()
        self.assertFalse(restored['manual'])
        self.assertEqual(restored['expression'],'生气','恢复最新状态，不恢复过时截图')
        self.assertEqual(restored['value'],80)

    def test_manual_cancel_and_repeat(self):
        self.apply()
        bridge=HeartBridge(self.store,['问号','开心兴奋'])
        bridge.manual('开心兴奋');first=bridge.state()['command_id']
        bridge.manual('开心兴奋');self.assertNotEqual(first,bridge.state()['command_id'])
        bridge.manual(None)
        self.assertEqual(bridge.state()['expression'],'问号')
        self.assertFalse(bridge.state()['manual'])
        with self.assertRaises(ValueError):bridge.manual('不存在')

    def test_manual_before_first_chat_does_not_invent_mood(self):
        now=[0]
        bridge=HeartBridge(self.store,['平静','开心兴奋'],clock=lambda:now[0])
        self.assertFalse(bridge.state()['ready'])
        bridge.manual('开心兴奋')
        state=bridge.state()
        self.assertTrue(state['ready']);self.assertIsNone(state['value'])
        now[0]=10
        self.assertEqual(bridge.state()['expression'],'平静')

    def test_manual_receipt_records_visual_reason_not_fake_emotion(self):
        self.apply()
        bridge=HeartBridge(self.store,['问号','开心兴奋'])
        bridge.manual('开心兴奋');state=bridge.state()
        bridge.receipt(AvatarReceipt(command_id=state['command_id'],client_id='browser',status='applied'))
        with self.store.connect() as db:
            rows=[json.loads(r[0]) for r in db.execute("SELECT payload FROM events WHERE kind='Live2D动作'")]
        self.assertIn('用户手动请求',rows[-1]['reason'])
        self.assertEqual(rows[-1]['expression'],'开心兴奋')
        self.assertEqual(rows[-1]['emotion']['label'],'疑惑')

    def test_config_webui_and_disable_clears_with_log(self):
        from maibot_sdk.config import generate_plugin_config_schema
        schema=generate_plugin_config_schema(plugin.Config)
        self.assertEqual(schema['sections']['emotions']['fields']['entries']['item_type'],'object')
        self.apply()
        instance=plugin.create_plugin();instance.store=self.store
        instance.set_plugin_config(self.config.model_dump())
        instance.config.emotions.enabled=False
        instance.sync_views()
        self.assertFalse(self.current()['active'])
        self.assertIn('临时情绪结束',self.kinds())

    def test_natural_request_separate_from_emotion_and_expires(self):
        self.apply()
        decision=self.assessment(action='keep')
        decision['display']={'action':'show','label':'生气','reason':'用户要求表演','confidence':.99,'evidence':'做个生气表情'}
        self.apply('n1',decision,text='做个生气表情')
        bridge=HeartBridge(self.store,['问号','生气'])
        state=bridge.state()
        self.assertTrue(state['performance'])
        self.assertEqual((state['expression'],state['label'],state['value']),('生气','疑惑',80))
        self.now+=timedelta(seconds=9)
        state=bridge.state()
        self.assertFalse(state['performance']);self.assertEqual(state['expression'],'问号')
        self.assertTrue(self.current()['active'])
        self.assertIn('表情表演请求',self.kinds())

    def test_natural_request_never_controls_web_avatar_from_qq(self):
        self.apply()
        decision=self.assessment(action='keep')
        decision['display']={'action':'show','label':'生气','reason':'群聊请求','confidence':.99,'evidence':'做个生气表情'}
        self.apply('q1',decision,session='qq',platform='qq',user='qq-user',text='做个生气表情')
        state=HeartBridge(self.store,['问号','生气']).state()
        self.assertFalse(state['performance']);self.assertEqual(state['expression'],'问号')

    def test_manual_cancel_suppresses_existing_ai_display(self):
        decision=self.assessment(action='keep')
        decision['display']={'action':'show','label':'生气','reason':'表演','confidence':.99,'evidence':'看不懂'}
        self.apply(decision=decision)
        bridge=HeartBridge(self.store,['平静','星星眼','生气'])
        self.assertTrue(bridge.state()['performance'])
        bridge.manual(None)
        self.assertFalse(bridge.state()['performance'])
        self.assertEqual(bridge.state()['expression'],'星星眼')

    def test_display_parser_rejects_bad_request_without_breaking_mood(self):
        raw={'delta':0,'confidence':.9,'reason':'普通请求','evidence':'','emotion':self.assessment()['emotion'],
             'display':{'action':'show','label':'生气','confidence':.99,'reason':'表演','evidence':'看不懂'}}
        parse=lambda:parse_assessment(json.dumps(raw,ensure_ascii=False),'看不懂',.7,self.config.emotions)
        self.assertEqual(parse()['display']['label'],'生气')
        for field,value in [('label','陌生表情'),('confidence',.5),('evidence','不是原话'),('action','delete')]:
            previous=raw['display'][field];raw['display'][field]=value
            result=parse();self.assertIn('display_error',result);self.assertEqual(result['delta'],0)
            raw['display'][field]=previous

    def test_disable_expression_requests_clears_existing_performance(self):
        decision=self.assessment(action='keep')
        decision['display']={'action':'show','label':'生气','reason':'表演','confidence':.99,'evidence':'看不懂'}
        self.apply(decision=decision)
        instance=plugin.create_plugin();instance.store=self.store
        instance.set_plugin_config(self.config.model_dump())
        instance.config.emotions.allow_expression_requests=False;instance.sync_views()
        self.assertFalse(HeartBridge(self.store,['星星眼','生气']).state()['performance'])

    def test_reply_prompt_uses_confirmed_emotion_in_qq_too(self):
        self.apply(platform='qq',user='qq-user')
        instance=plugin.create_plugin();instance.store=self.store;instance.engine=self.engine
        instance.set_plugin_config(self.config.model_dump())
        result=asyncio.run(instance.before_reply(session_id='live',reply_message_id='m1'))
        prompt=result['modified_kwargs']['extra_prompt']
        self.assertIn('疑惑',prompt)
        self.assertIn('80',prompt)
        self.assertIn('如果用户问你现在的情绪',prompt)

    def test_http_guards_and_receipt(self):
        import httpx
        from fastapi import FastAPI
        from heart_bridge import create_router
        self.apply()
        app=FastAPI();app.include_router(create_router(self.store,['问号','星星眼']))
        async def run():
            transport=httpx.ASGITransport(app=app,client=('127.0.0.1',1234))
            async with httpx.AsyncClient(transport=transport,base_url='http://127.0.0.1:12393') as client:
                response=await client.get('/heart/state');self.assertEqual(response.status_code,200)
                state=response.json()
                self.assertNotIn('session_id',state)
                for path in ('/heart/state',):
                    self.assertEqual((await client.get(path,headers={'origin':'https://evil.example'})).status_code,403)
                body={'command_id':state['command_id'],'client_id':'test','status':'applied'}
                self.assertEqual((await client.post('/heart/avatar-events',json=body)).status_code,403)
                self.assertEqual((await client.post('/heart/avatar-events',json=body,
                    headers={'x-heart-client':state['csrf']})).status_code,200)
                self.assertEqual((await client.post('/heart/avatar-events',json={**body,'command_id':'not-issued'},
                    headers={'x-heart-client':state['csrf']})).status_code,409)
        asyncio.run(run())


class TopicAIIntegration(unittest.IsolatedAsyncioTestCase):
    async def test_one_call_scoring_and_topic_then_clear(self):
        store=AuditStore(ROOT/'.runtime/test-data'/uuid.uuid4().hex)
        config=plugin.Config(ai=plugin.AISettings(enabled=True),emotions=plugin.TopicEmotions(enabled=True))
        calls=[]
        answer={'delta':0,'confidence':.9,'reason':'分数不变','evidence':'',
                'emotion':{'action':'set','label':'疑惑','intensity':.6,'topic':'证明',
                           'reason':'暂不理解问题','confidence':.9,'evidence':'这题'}}
        async def call(*args,**kwargs):
            calls.append(kwargs['prompt'])
            return {'success':True,'response':json.dumps(answer,ensure_ascii=False)}
        p=SimpleNamespace(store=store,engine=MoodEngine(store),config=config,
            ctx=SimpleNamespace(call_capability=call),_get_logger=lambda:logging.getLogger('test'))
        ai=AIAppraiser(p)
        try:
            message={'session_id':'s','message_id':'1','processed_plain_text':'这题怎么做',
                     'message_info':{'platform':'qq','user_info':{'user_id':'x','user_nickname':'测试'}}}
            await ai.submit(message);await asyncio.wait_for(ai.queue.join(),3)
            self.assertEqual(len(calls),1)
            with store.connect() as db:self.assertTrue(store.emotion_snapshot(db,'s')['active'])
            answer['emotion'].update(action='clear',reason='题目已解决',evidence='解决了')
            await ai.submit({**message,'message_id':'2','processed_plain_text':'解决了，换个话题'})
            await asyncio.wait_for(ai.queue.join(),3)
            with store.connect() as db:self.assertFalse(store.emotion_snapshot(db,'s')['active'])
            self.assertIn('证明',calls[-1])
            self.assertTrue(await ai.wait_session('s',0))
        finally:await ai.close()


if __name__=='__main__':unittest.main()
