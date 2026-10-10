"""0.3：用户编辑、房间记忆、旁听和自主轮流讨论；临时数据，不调用收费模型。"""
from pathlib import Path
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import AsyncMock
import copy
import json
import tempfile
import unittest
import sqlite3

import test_storyroom as baseline


class UpgradeTests(unittest.TestCase):
    setUp = baseline.PlatformTests.setUp
    snapshot = baseline.PlatformTests.snapshot
    request_task = baseline.PlatformTests.request_task
    propose = baseline.PlatformTests.propose

    def post(self, suffix, data):
        return self.client.post(self.prefix + suffix, headers=self.host, json=data)

    def move_time(self, seconds=20):
        now = self.client.app.state.store.clock()
        self.client.app.state.store.clock = lambda: now + seconds

    def autonomous(self, intent, player, speech='先和同伴商量'):
        task = self.client.get('/api/bot/task', headers=self.pc1 if player == 0 else self.pc2).json()['task']
        self.assertIsNotNone(task)
        result = self.client.post('/api/bot/tasks/' + task['id'] + '/result', headers=self.pc1 if player == 0 else self.pc2,
            json={'lease': task['lease'], 'speech': speech, 'action': '调查门后，期待找出声源' if intent == 'action' else '', 'emotion': '疑惑', 'intent': intent})
        self.assertEqual(result.status_code, 200)
        return task

    def test_custom_template_persists_and_existing_room_frozen(self):
        original = self.snapshot()['world']
        item = self.client.get('/api/stories', headers=self.host).json()[0]
        item['script']['id'] = 'our-story'
        item['script']['title'] = '我们自己的故事'
        item['script']['world'] = '无魔法的校园调查，规则由我们指定'
        r = self.client.post('/api/stories/save', headers=self.host, json={'script': item['script']})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.snapshot()['world'], original)
        from storyroom.app import create_app
        from fastapi.testclient import TestClient
        reloaded = TestClient(create_app(Path(self.temp.name), 'isolated-host-token'))
        made = reloaded.post('/api/rooms', headers=self.host, json={'story': 'our-story'}).json()
        new = reloaded.get('/api/rooms/' + made['room_id'], headers=self.host).json()
        self.assertEqual(new['world'], item['script']['world'])

    def test_v02_database_upgrade_keeps_room_script_memory_keys_and_backup(self):
        from storyroom.store import RoomStore, digest
        with tempfile.TemporaryDirectory() as root:
            root=Path(root)
            script=json.loads((Path(__file__).resolve().parents[1]/'stories/lighthouse.json').read_text(encoding='utf-8'))
            raw=json.dumps(script,ensure_ascii=False)
            with closing(sqlite3.connect(root/'rooms.sqlite3')) as db:
                db.execute('CREATE TABLE rooms(id TEXT PRIMARY KEY,name TEXT,story TEXT,node TEXT,flags TEXT,version INTEGER,created REAL,script TEXT,script_hash TEXT)')
                db.execute('INSERT INTO rooms VALUES(?,?,?,?,?,?,?,?,?)',('old','旧团','lighthouse','workshop','["gear_found"]',7,0,raw,digest(raw)))
                db.execute('CREATE TABLE roles(id TEXT PRIMARY KEY,room TEXT,name TEXT,key_hash TEXT UNIQUE,avatar TEXT,visual TEXT,trust INTEGER DEFAULT 0,last_seen REAL DEFAULT 0,card TEXT,mood INTEGER)')
                db.execute('INSERT INTO roles VALUES(?,?,?,?,?,?,?,?,?,?)',('role','old','旧角色',digest('isolated-old-player-key'),'none','{"emotion":"平静","revision":"old"}',1,0,'{}',60))
                db.execute('CREATE TABLE experiences(id TEXT PRIMARY KEY,room TEXT,role TEXT,task TEXT UNIQUE,node TEXT,text TEXT,active INTEGER DEFAULT 1,created REAL)')
                db.execute('INSERT INTO experiences VALUES(?,?,?,?,?,?,?,?)',('memory','old','role','task','harbor','旧团经历保留',1,0))
                db.commit()
            store=RoomStore(root,Path(__file__).resolve().parents[1]/'stories')
            state=store.snapshot('old')
            self.assertEqual(state['script_hash'],digest(raw));self.assertEqual(state['node'],'workshop')
            self.assertEqual(state['version'],7);self.assertEqual(state['flags'],['gear_found'])
            self.assertEqual(state['experiences'][0]['text'],'旧团经历保留')
            self.assertEqual(store.role('isolated-old-player-key')['mood'],60)
            self.assertFalse(state['talk']['opened'])
            self.assertEqual(len(list((root/'备份').glob('升级0.3前-*.sqlite3'))),1)

    def test_template_edit_conflict_and_player_permissions(self):
        item = self.client.get('/api/stories', headers=self.host).json()[0]
        self.assertEqual(self.client.post('/api/stories/save', headers=self.host, json={'script': item['script']}).status_code, 409)
        self.assertEqual(self.client.post('/api/stories/save', headers=self.pc1, json={'script': item['script']}).status_code, 403)
        self.assertEqual(self.client.get(self.prefix + '/script', headers=self.pc1).status_code, 403)

    def test_invalid_branches_ending_ids_cycles_rejected(self):
        item = self.client.get('/api/stories', headers=self.host).json()[0]
        for mutate in (lambda s: s['nodes']['harbor']['next'].append({'id':'nowhere','label':'跳出'}),
                       lambda s: s['nodes']['dark']['next'].append({'id':'tower','label':'另起主线'}),
                       lambda s: s.update(id='../outside'),
                       lambda s: s.update(start='absent'),
                       lambda s: s['nodes']['harbor'].update(next=[{'id':'harbor','label':'死循环'}])):
            script = copy.deepcopy(item['script']);mutate(script)
            r = self.client.post('/api/stories/save', headers=self.host, json={'script':script,'expected_hash':item['hash']})
            self.assertEqual(r.status_code, 422)

    def test_apply_requires_explicit_version_and_no_pending_task(self):
        item = self.client.get(self.prefix + '/script', headers=self.host).json()
        item['script']['world'] = '同一故事的人类修订'
        self.assertEqual(self.post('/script', {'script':item['script'],'expected_hash':'wrong'}).status_code, 409)
        task = self.request_task()
        self.assertEqual(self.post('/script', {'script':item['script'],'expected_hash':item['hash']}).status_code, 409)
        self.post('/tasks/' + task['id'] + '/cancel', {})
        self.assertEqual(self.post('/script', {'script':item['script'],'expected_hash':item['hash']}).status_code, 200)
        self.assertEqual(self.snapshot()['world'], '同一故事的人类修订')
        self.assertEqual(self.snapshot()['node'], 'harbor')

    def test_story_and_player_memory_are_room_isolated(self):
        self.post('/memory', {'subject':'遗留线索','text':'蓝色钥匙在阁楼'})
        self.post('/participants', {'name':'阿岚','profile':'真人玩家，怕火，认识老船长'})
        task = self.request_task()
        wire = json.dumps(task, ensure_ascii=False)
        self.assertIn('蓝色钥匙在阁楼', wire);self.assertIn('怕火', wire)
        self.assertTrue(task['memory_namespace'].startswith('storyroom:' + self.room + ':'))
        other = self.client.post('/api/rooms', headers=self.host, json={}).json()
        self.client.post('/api/rooms/'+other['room_id']+'/turn', headers=self.host,json={'role':other['connections'][0]['id']})
        private = self.client.get('/api/bot/task',headers={'Authorization':'Bearer '+other['connections'][0]['token']}).json()
        self.assertNotIn('蓝色钥匙', json.dumps(private,ensure_ascii=False))

    def test_correct_and_forget_memory_keep_old_audit_not_prompt(self):
        identifier = self.post('/memory', {'text':'唯一错误线索-黑灯'}).json()['id']
        self.post('/memory', {'id':identifier,'text':'唯一正确信息-白灯'})
        task = self.request_task();wire=json.dumps(task,ensure_ascii=False)
        self.assertNotIn('唯一错误',wire);self.assertIn('唯一正确',wire)
        self.post('/tasks/'+task['id']+'/cancel',{})
        self.post('/memory', {'id':identifier,'text':'唯一正确信息-白灯','active':False})
        task = self.request_task();self.assertNotIn('唯一正确',json.dumps(task,ensure_ascii=False))
        self.assertIn('唯一错误',json.dumps(self.snapshot()['events'],ensure_ascii=False))

    def test_confirmed_narration_automatically_remembered_and_recall_old_fact(self):
        self.post('/narrate', {'text':'紫水晶钥匙由老船长保管'})
        for i in range(25):self.post('/narrate', {'text':f'无关天气记录{i}'})
        self.post('/turn',{'role':self.players[0]['id'],'question':'紫水晶钥匙现在在哪里？'})
        task=self.client.get('/api/bot/task',headers=self.pc1).json()['task']
        self.assertIn('紫水晶钥匙由老船长保管',json.dumps(task['room_memory'],ensure_ascii=False))

    def test_withdraw_narration_does_not_leak_via_history(self):
        self.post('/narrate', {'text':'错误故事-独特的幽灵'})
        note=self.snapshot()['room_memory']['notes'][0]
        self.post('/memory',{**note,'text':'修正故事-只是一阵风','active':True})
        task=self.request_task();wire=json.dumps(task,ensure_ascii=False)
        self.assertNotIn('独特的幽灵',wire);self.assertIn('只是一阵风',wire)
        self.post('/tasks/'+task['id']+'/cancel',{})
        self.post('/memory',{**note,'active':False})
        self.assertNotIn('只是一阵风',json.dumps(self.request_task(),ensure_ascii=False))

    def test_waits_for_host_then_round_robin_discussion_and_budget(self):
        self.assertIsNone(self.client.get('/api/bot/task',headers=self.pc1).json()['task'])
        self.post('/host-speech',{'client':'gm','speaking':True})
        self.post('/talk',{'opened':True,'limit':2})
        self.assertIsNone(self.client.get('/api/bot/task',headers=self.pc1).json()['task'])
        self.post('/host-speech',{'client':'gm','speaking':False});self.move_time(2)
        first=self.autonomous('discussion',0,'我建议先观察门，伙伴你觉得呢？')
        self.assertIsNone(self.client.get('/api/bot/task',headers=self.pc2).json()['task'])
        self.move_time()
        second=self.autonomous('discussion',1,'我同意先观察，留意门框的痕迹')
        self.assertIn('伙伴你觉得呢',json.dumps(second['conversation'],ensure_ascii=False))
        self.move_time()
        self.assertIsNone(self.client.get('/api/bot/task',headers=self.pc1).json()['task'])
        self.assertEqual(self.snapshot()['talk']['remaining'],0)
        self.assertEqual(self.snapshot()['experiences'],[])

    def test_action_pauses_until_gm_confirms_actual_consequence_then_rethinks(self):
        self.post('/talk',{'opened':True,'limit':4})
        first=self.autonomous('action',0)
        self.move_time()
        self.assertIsNone(self.client.get('/api/bot/task',headers=self.pc2).json()['task'])
        self.assertEqual(self.snapshot()['flags'],[])
        self.post('/tasks/'+first['id']+'/decide',{'ruling':'门上锁了，调查未成功','mood_delta':-5})
        self.move_time(2)
        second=self.autonomous('discussion',1)
        self.assertIn('门上锁了',json.dumps(second,ensure_ascii=False))
        self.assertEqual(self.snapshot()['experiences'][0]['text'],'门上锁了，调查未成功')

    def test_question_waits_for_gm_answer_then_uses_new_information(self):
        self.post('/talk',{'opened':True,'limit':4})
        first=self.autonomous('question',0,'主持人，门缝里是否有光？')
        self.move_time();self.assertIsNone(self.client.get('/api/bot/task',headers=self.pc2).json()['task'])
        self.assertEqual(self.post('/tasks/'+first['id']+'/answer',{'text':'门缝有一线暖黄色灯光'}).status_code,200)
        self.move_time(2)
        second=self.autonomous('discussion',1)
        self.assertIn('暖黄色灯光',json.dumps(second,ensure_ascii=False))
        self.assertEqual(self.snapshot()['experiences'],[])

    def test_host_speaks_during_thinking_result_held_without_losing_it(self):
        task=self.request_task()
        self.post('/host-speech',{'client':'gm','speaking':True})
        self.assertEqual(self.propose(task).status_code,200)
        self.assertTrue(self.snapshot()['tasks'][0]['held']);self.assertIsNone(self.snapshot()['tasks'][0]['result'])
        self.post('/host-speech',{'client':'gm','speaking':False})
        self.assertEqual(self.snapshot()['tasks'][0]['result']['speech'],'我试着找齿轮')

    def test_audio_lease_serializes_browsers_and_stops_next_robot(self):
        self.post('/talk',{'opened':True,'limit':3});first=self.autonomous('discussion',0)
        self.assertEqual(self.post('/audio-lease',{'client':'browser-one','task':first['id'],'playing':True}).status_code,200)
        self.assertEqual(self.post('/audio-lease',{'client':'browser-two','task':first['id'],'playing':True}).status_code,409)
        self.move_time(20);self.assertIsNone(self.client.get('/api/bot/task',headers=self.pc2).json()['task'])
        self.post('/audio-lease',{'client':'browser-one','task':first['id'],'playing':False});self.move_time(2)
        self.assertIsNotNone(self.client.get('/api/bot/task',headers=self.pc2).json()['task'])

    def test_host_hold_expires_after_disconnection(self):
        self.post('/host-speech',{'client':'gm','speaking':True});self.post('/talk',{'opened':True,'limit':2})
        self.move_time(91)
        self.assertIsNone(self.client.get('/api/bot/task',headers=self.pc1).json()['task'])
        self.assertFalse(self.snapshot()['talk']['opened'])
        self.post('/talk',{'opened':True,'limit':2})  # 主持人显式确认恢复，不能把断线当作已经讲完。
        self.assertIsNotNone(self.client.get('/api/bot/task',headers=self.pc1).json()['task'])

    def test_observe_catches_all_public_pages_without_private_prompt_or_other_room(self):
        store=self.client.app.state.store
        with store.db() as db:
            for i in range(180):store.event(db,self.room,'玩家交流','同伴',{'发言':f'旁听{i}'})
            store.event(db,self.room,'本次上下文与回忆','同伴',{'私有资料':'不可传给观察接口'})
        other=self.client.post('/api/rooms',headers=self.host,json={}).json()['room_id']
        self.client.post('/api/rooms/'+other+'/dialogue',headers=self.host,json={'text':'另一个房间的秘密'})
        cursor=0;events=[]
        for _ in range(10):
            r=self.client.get('/api/bot/observe?after='+str(cursor),headers=self.pc1).json()
            self.assertGreaterEqual(r['cursor'],cursor);cursor=r['cursor'];events.extend(r['events'])
            if not r['more']:break
        wire=json.dumps(events,ensure_ascii=False)
        self.assertIn('旁听0',wire);self.assertIn('旁听179',wire)
        self.assertNotIn('不可传给观察接口',wire);self.assertNotIn('另一个房间的秘密',wire)
        self.assertEqual(self.snapshot()['tasks'],[])

    def test_oversized_observer_pages_are_bounded_not_truncated_or_lost(self):
        store=self.client.app.state.store
        with store.db() as db:
            for i in range(64):store.event(db,self.room,'玩家交流','同伴',{'发言':str(i)+'字'*2000})
        cursor=0;count=0
        for _ in range(10):
            r=self.client.get('/api/bot/observe?after='+str(cursor),headers=self.pc1)
            self.assertLess(len(r.content),70000)
            data=r.json();cursor=data['cursor'];count+=sum(e['kind']=='玩家交流' for e in data['events'])
            if not data['more']:break
        self.assertEqual(count,64)

    def test_pc_cannot_change_memory_players_discussion_or_host_floor(self):
        for suffix,data in (('/memory',{'text':'作弊'}),('/participants',{'name':'作弊'}),('/talk',{'opened':True}),('/host-speech',{'client':'pc','speaking':True})):
            self.assertEqual(self.client.post(self.prefix+suffix,headers=self.pc1,json=data).status_code,403)

    def test_finished_story_cannot_reopen_autonomous_mainline(self):
        self.post('/advance',{'target':'tower'});self.post('/advance',{'target':'dark'})
        self.assertEqual(self.post('/talk',{'opened':True}).status_code,409)
        self.assertIsNone(self.client.get('/api/bot/task',headers=self.pc1).json()['task'])

    def test_discussion_cannot_smuggle_executed_action(self):
        task=self.request_task()
        r=self.client.post('/api/bot/tasks/'+task['id']+'/result',headers=self.pc1,json={'lease':task['lease'],'speech':'讨论','action':'已破门','intent':'discussion'})
        self.assertEqual(r.status_code,422)

    def test_memory_edit_cancels_running_context_instead_of_blocking_room(self):
        task=self.request_task()
        self.post('/memory',{'text':'主持人修正：门现在打开了'})
        self.assertEqual(self.snapshot()['tasks'][0]['status'],'cancelled')
        self.assertEqual(self.propose(task).status_code,409)
        next_task=self.request_task()
        self.assertIn('门现在打开了',json.dumps(next_task,ensure_ascii=False))

    def test_pausing_cancels_unclaimed_autonomous_task_without_model_call(self):
        self.post('/talk',{'opened':True,'limit':4})
        # 轮询的不是下一位：只创建队列，没有开始调用模型。
        self.assertIsNone(self.client.get('/api/bot/task',headers=self.pc2).json()['task'])
        self.post('/talk',{'opened':False,'limit':4})
        self.assertIsNone(self.client.get('/api/bot/task',headers=self.pc1).json()['task'])
        self.assertEqual(self.snapshot()['tasks'][0]['status'],'cancelled')

    def test_offline_next_player_queue_times_out_and_pauses_room(self):
        self.post('/talk',{'opened':True,'limit':4})
        self.client.get('/api/bot/task',headers=self.pc2)
        self.move_time(121)
        self.assertIsNone(self.client.get('/api/bot/task',headers=self.pc2).json()['task'])
        self.assertFalse(self.snapshot()['talk']['opened'])
        self.assertEqual(self.snapshot()['tasks'][0]['status'],'failed')

    def test_cohost_can_edit_room_script_but_cannot_overwrite_global_templates(self):
        invited=self.post('/hosts',{'name':'伙伴GM'}).json()
        key={'Authorization':'Bearer '+invited['token']}
        item=self.client.get(self.prefix+'/script',headers=key).json()
        item['script']['title']='伙伴为本房间修订的标题'
        self.assertEqual(self.client.post(self.prefix+'/script',headers=key,json={'script':item['script'],'expected_hash':item['hash']}).status_code,200)
        self.assertEqual(self.client.get('/api/stories',headers=key).status_code,403)

    def test_other_players_confirmed_experience_is_shared_story_not_proposal(self):
        task=self.request_task();self.propose(task)
        self.post('/tasks/'+task['id']+'/decide',{'ruling':'月铃已经发现门锁生锈'})
        second=self.request_task(1)
        self.assertIn('门锁生锈',json.dumps(second['shared_experiences'],ensure_ascii=False))
        self.assertEqual(second['memories'],[])  # 自己的经历仍按角色隔离。


class ObservationTests(unittest.IsolatedAsyncioTestCase):
    async def test_cursor_persists_restart_and_binding_isolation_without_model(self):
        pc=baseline.worker_module
        with tempfile.TemporaryDirectory() as root:
            reply={'room':'room-one','role':'role-one','cursor':3,'more':False,'events':[{'kind':'玩家交流','detail':{'发言':'我看见船了'}}],
                   'memory_namespace':'storyroom:room-one','memory_revision':2,'talk':{}}
            client=SimpleNamespace(binding='binding-one',request=AsyncMock(return_value=reply))
            model=AsyncMock()
            worker=pc.PCWorker(client,model,'','',2,lambda _:None,data_dir=root,audit=pc.Audit(Path(root)/'日志'))
            await worker.observe_once();model.assert_not_called()
            resumed=pc.PCWorker(client,model,'','',2,lambda _:None,data_dir=root)
            await resumed.observe_once();self.assertIn('after=3',client.request.call_args.args[0])
            other=SimpleNamespace(binding='binding-two',request=AsyncMock(return_value={**reply,'room':'room-two'}))
            await pc.PCWorker(other,model,'','',2,lambda _:None,data_dir=root).observe_once()
            self.assertIn('after=0',other.request.call_args.args[0])
            audit=''.join(p.read_text(encoding='utf-8') for p in (Path(root)/'日志').glob('*.jsonl'))
            self.assertIn('持续旁听',audit);self.assertIn('"model_called": false',audit)

    def test_intent_schema_legacy_compatible_but_invalid_discussion_rejected(self):
        pc=baseline.worker_module
        self.assertEqual(pc.parse_reply('{"speech":"继续","action":"调查","emotion":"疑惑"}')['action'],'调查')
        with self.assertRaises(ValueError):pc.parse_reply('{"speech":"商量","action":"执行行动","emotion":"疑惑","intent":"discussion"}')
