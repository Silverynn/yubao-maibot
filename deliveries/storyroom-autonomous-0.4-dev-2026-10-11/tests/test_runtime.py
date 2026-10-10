"""No real model calls. Test transaction boundaries, branches and authority."""
import json
import tempfile
import unittest
from pathlib import Path
from fastapi.testclient import TestClient
from storyroom.app import create_app
from storyroom.store import actor_id, actor_name, RoomError, RoomStore
from storyroom.gm import GMReply

ROOT=Path(__file__).resolve().parents[1]


class RuntimeBase:
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.client=TestClient(create_app(Path(self.tmp.name),'runtime-host-test'))
        self.host={'Authorization':'Bearer runtime-host-test'}
        self.store=self.client.app.state.store
        made=self.client.post('/api/rooms',headers=self.host,json={}).json()
        self.room=made['room_id'];self.players=made['connections'];self.prefix='/api/rooms/'+self.room
        self.pc={'Authorization':'Bearer '+self.players[0]['token']}
        self.owner=self.store.role(self.players[0]['token'])

    def view(self):return self.client.get(self.prefix,headers=self.host).json()
    def control(self, op):
        return self.client.post(self.prefix+'/runtime',headers=self.host,json={'operation':op,'revision':self.view()['runtime']['revision']})
    def running(self):
        self.assertEqual(self.control('takeover').status_code,200)
        self.assertEqual(self.control('resume').status_code,200)
    def turn(self):
        response=self.client.post(self.prefix+'/turn',headers=self.host,json={'role':self.players[0]['id'],'question':'调查工作台'})
        self.assertEqual(response.status_code,200,response.text)
        return self.client.get('/api/bot/task',headers=self.pc).json()['task']
    def complete(self,t):
        return self.client.post('/api/bot/tasks/'+t['id']+'/result',headers=self.pc,json={'lease':t['lease'],'speech':'我找齿轮。','action':'调查工作台','emotion':'开心','intent':'action'})



class RuntimeTests(RuntimeBase, unittest.TestCase):
    def test_pause_rejects_late_result_and_claim(self):
        self.running();t=self.turn();self.assertEqual(self.control('pause').status_code,200)
        self.assertEqual(self.complete(t).status_code,409)
        self.assertIsNone(self.client.get('/api/bot/task',headers=self.pc).json()['task'])
        self.assertEqual(self.client.post(self.prefix+'/narrate',headers=self.host,json={'text':'不应发布'}).status_code,409)
        self.assertEqual(self.control('resume').status_code,200)
        next_task=self.client.get('/api/bot/task',headers=self.pc).json()['task']
        self.assertNotEqual(next_task['lease'],t['lease'])

    def test_pc_cannot_control_or_rewind(self):
        self.assertEqual(self.client.post(self.prefix+'/runtime',headers=self.pc,json={'operation':'pause','revision':1}).status_code,403)
        self.assertEqual(self.client.get(self.prefix+'/timeline',headers=self.pc).status_code,403)

    def test_revision_prevents_double_control(self):
        rev=self.view()['runtime']['revision']
        self.control('takeover')
        self.assertEqual(self.client.post(self.prefix+'/runtime',headers=self.host,json={'operation':'pause','revision':rev}).status_code,409)

    def test_two_hosts_cannot_own_controller(self):
        self.running()
        key=self.client.post(self.prefix+'/hosts',headers=self.host,json={'name':'同伴'}).json()['token']
        other={'Authorization':'Bearer '+key}
        self.assertEqual(self.client.post(self.prefix+'/runtime',headers=other,json={'operation':'takeover','revision':self.view()['runtime']['revision']}).status_code,409)
        self.assertEqual(self.client.post(self.prefix+'/narrate',headers=other,json={'text':'抢先裁决'}).status_code,409)
        self.control('release')
        self.assertEqual(self.client.post(self.prefix+'/runtime',headers=other,json={'operation':'takeover','revision':self.view()['runtime']['revision']}).status_code,200)

    def test_rewind_restores_facts_and_preserves_archive(self):
        self.running()
        self.client.post(self.prefix+'/narrate',headers=self.host,json={'text':'旧现场：门关着。'})
        cp=self.client.get(self.prefix+'/timeline',headers=self.host).json()['checkpoints'][0]['id']
        oldbranch=self.view()['runtime']['branch']
        self.client.post(self.prefix+'/narrate',headers=self.host,json={'text':'未来秘密：门后是银色宝箱。'})
        self.control('pause')
        r=self.client.post(self.prefix+'/rewind',headers=self.host,json={'checkpoint':cp,'revision':self.view()['runtime']['revision']})
        self.assertEqual(r.status_code,200,r.text)
        v=self.view();self.assertEqual(v['runtime']['state'],'PAUSED');self.assertNotEqual(v['runtime']['branch'],oldbranch)
        current=self.client.get(self.prefix+'/script.md',headers=self.host).text
        archive=self.client.get(self.prefix+'/script.md?branch='+oldbranch,headers=self.host).text
        self.assertIn('门关着',current);self.assertNotIn('银色宝箱',current);self.assertIn('银色宝箱',archive)
        self.assertNotIn('银色宝箱',json.dumps(v['room_memory'],ensure_ascii=False))
        self.control('takeover');self.control('resume');t=self.turn()
        self.assertNotIn('银色宝箱',json.dumps(t,ensure_ascii=False))

    def test_cross_room_checkpoint_is_rejected(self):
        other=self.client.post('/api/rooms',headers=self.host,json={}).json()['room_id']
        cp=self.client.get('/api/rooms/'+other+'/timeline',headers=self.host).json()['checkpoints'][0]['id']
        self.running();self.control('pause')
        self.assertEqual(self.client.post(self.prefix+'/rewind',headers=self.host,json={'checkpoint':cp,'revision':self.view()['runtime']['revision']}).status_code,404)

    def test_restart_preserves_branch_and_pauses(self):
        self.running();branch=self.view()['runtime']['branch']
        restarted=RoomStore(Path(self.tmp.name),ROOT/'stories')
        v=restarted.snapshot(self.room)
        self.assertEqual(v['runtime']['state'],'PAUSED');self.assertEqual(v['runtime']['branch'],branch)
        self.assertEqual(v['runtime']['gm_mode'],'HUMAN');self.assertEqual(v['runtime']['controller_until'],0)

    def test_handoff_requires_model_and_does_not_unpause(self):
        self.running();self.control('pause')
        self.assertEqual(self.control('handoff').status_code,409)
        self.store.gm_configured=True
        self.assertEqual(self.control('handoff').status_code,200)
        self.assertEqual(self.view()['runtime']['state'],'PAUSED')

    def test_takeover_invalidates_gm_request(self):
        self.running();self.store.gm_configured=True;self.control('handoff')
        with self.store.db() as db:db.execute('UPDATE room_talk SET opened=0 WHERE room=?',(self.room,))
        ticket=self.store.claim_gm(self.room);self.assertIsNotNone(ticket)
        self.control('takeover');self.assertFalse(self.store.gm_current(ticket))
        with self.assertRaises(RoomError):self.store.complete_gm(ticket,GMReply(operation='narrate',text='过期主持台词'))
        self.assertNotIn('过期主持台词',self.store.script_export(self.room))

    def test_gm_result_is_atomic_and_tracked(self):
        self.running();self.store.gm_configured=True;self.control('handoff')
        with self.store.db() as db:db.execute('UPDATE room_talk SET opened=0 WHERE room=?',(self.room,))
        ticket=self.store.claim_gm(self.room)
        self.store.complete_gm(ticket,GMReply(operation='narrate',text='门上的锁发出了轻响。'))
        self.assertIn('AI主持人',self.store.script_export(self.room))
        self.assertEqual(self.view()['runtime']['gm_calls'],1)
        with self.assertRaises(RoomError):self.store.complete_gm(ticket,GMReply(operation='narrate',text='重复'))

    def test_temporary_expression_returns_to_neutral(self):
        self.running();t=self.turn();self.complete(t)
        self.assertEqual(self.view()['roles'][0]['visual']['emotion'],'开心')
        original=self.store.clock;self.store.clock=lambda:original()+6
        self.assertEqual(self.view()['roles'][0]['visual']['emotion'],'平静')

    def test_checkpoint_does_not_store_credentials(self):
        with self.store.db() as db:
            data=db.execute('SELECT payload FROM checkpoints WHERE room=?',(self.room,)).fetchone()[0]
        self.assertNotIn(self.players[0]['token'],data);self.assertNotIn('key_hash',data);self.assertNotIn('runtime-host-test',data)

    def test_three_simulated_ai_complete_story_with_fu_checks(self):
        """Stateful GM and two scripted PC models; no paid or remote calls."""
        now=[self.store.clock()];self.store.clock=lambda:now[0];self.store.roll=lambda n:5
        card={'attributes':{'DEX':8,'INS':8,'MIG':8,'WLP':8},'resources':{k:{'current':v,'max':v} for k,v in [('HP',45),('MP',45),('IP',6),('FP',3)]},'traits':[]}
        for role in self.players:self.store.set_fu_card(self.room,role['id'],card)
        self.running();self.store.gm_configured=True;self.control('handoff')
        owners=[self.store.role(p['token']) for p in self.players]
        def discuss():
            for _ in range(4):
                now[0]+=20
                task=None
                for owner in owners:
                    task=self.store.claim(owner)
                    if task:break
                self.assertIsNotNone(task)
                self.store.complete(owner,task['id'],task['lease'],{'speech':'【模拟PC】我们观察现场后继续前进。','action':'','emotion':'平静','intent':'discussion'})
        def gm(**fields):
            now[0]+=20
            ticket=self.store.claim_gm(self.room);self.assertIsNotNone(ticket)
            self.store.complete_gm(ticket,GMReply(**fields))
        def action(flag):
            now[0]+=20
            task=None
            for owner in owners:
                task=self.store.claim(owner)
                if task:break
            self.assertIsNotNone(task)
            self.store.complete(owner,task['id'],task['lease'],{'speech':'【模拟PC】我调查并处理眼前的问题。','action':'观察并修复现场','emotion':'平静','intent':'action'})
            gm(operation='check',task=task['id'],attributes=['DEX','INS'],difficulty=10)
            self.store.claim_fu_choice(owner) # No traits: no extra model needed.
            gm(operation='resolve',task=task['id'],text='【模拟GM】处理完成，新的事实已确认。',flags=[flag])
        discuss();gm(operation='advance',target='workshop')
        action('gear_found');discuss();gm(operation='advance',target='tower')
        action('lens_cleaned');discuss();gm(operation='advance',target='light')
        self.store.claim_gm(self.room)
        view=self.view();self.assertEqual(view['runtime']['state'],'FINISHED')
        self.assertEqual(set(view['flags']),{'gear_found','lens_cleaned'})
        play=self.store.script_export(self.room)
        for role in self.players:self.assertIn(role['name'],play)
        self.assertIn('AI主持人',play);self.assertIn('最终物语检定',play)


class FabulaTests(RuntimeBase, unittest.TestCase):
    def card(self):
        return {'attributes':{'DEX':8,'INS':10,'MIG':6,'WLP':8},'resources':{k:{'current':v,'max':m} for k,v,m in [('HP',35,35),('MP',45,45),('IP',6,6),('FP',3,99)]},'traits':['观察敏锐'],'notes':'隔离测试卡'}

    def test_player_choice_and_resource_rollback(self):
        card=self.card();self.assertEqual(self.client.post(self.prefix+'/roles/'+self.owner['id']+'/fabula',headers=self.host,json={'card':card}).status_code,200)
        self.running();t=self.turn();self.complete(t)
        cp=self.client.get(self.prefix+'/timeline',headers=self.host).json()['checkpoints'][0]['id']
        self.store.roll=lambda n:2
        r=self.client.post(self.prefix+'/tasks/'+t['id']+'/fabula-check',headers=self.host,json={'attributes':['DEX','INS'],'difficulty':10})
        self.assertEqual(r.status_code,200,r.text)
        self.assertFalse(r.json()['success'])
        choice=self.client.get('/api/bot/fabula-choice',headers=self.pc).json()['choice']
        self.assertIsNotNone(choice)
        data={'lease':choice['lease'],'indices':[1],'trait':'观察敏锐'}
        self.assertEqual(self.client.post('/api/bot/fabula-choice/'+t['id'],headers=self.pc,json=data).status_code,200)
        self.assertEqual(self.view()['rules']['cards'][0]['card']['resources']['FP']['current'],2)
        self.control('pause');self.client.post(self.prefix+'/rewind',headers=self.host,json={'checkpoint':cp,'revision':self.view()['runtime']['revision']})
        self.assertEqual(self.view()['rules']['cards'][0]['card']['resources']['FP']['current'],3)
        self.assertEqual(self.view()['rules']['checks'],[])

    def test_fumble_and_critical_results(self):
        from storyroom.fabula import evaluate
        self.assertFalse(evaluate([1,1],99,1)['success'])
        self.assertTrue(evaluate([6,6],0,30)['success'])
        self.assertFalse(evaluate([5,5],0,13)['success'])


class SceneTests(RuntimeBase, unittest.TestCase):
    def test_stale_background_is_cached_but_not_displayed(self):
        import base64,io
        from PIL import Image
        self.running();self.store.image_configured=True
        with self.store.db() as db:db.execute("UPDATE scenes SET status='queued' WHERE room=?",(self.room,))
        ticket=self.store.claim_scene();self.assertIsNotNone(ticket)
        buffer=io.BytesIO();Image.new('RGB',(2,2),'white').save(buffer,format='PNG')
        self.control('pause')
        result=self.store.complete_scene(ticket,base64.b64encode(buffer.getvalue()).decode())
        self.assertFalse(result['displayed']);self.assertEqual(self.view()['scene_image']['status'],'held')
        self.control('resume');self.assertEqual(self.view()['scene_image']['status'],'ready')

    def test_image_failure_does_not_mutate_story(self):
        self.running();self.store.image_configured=True
        with self.store.db() as db:db.execute("UPDATE scenes SET status='queued' WHERE room=?",(self.room,))
        ticket=self.store.claim_scene();before=self.view()['version']
        with self.assertRaises(RoomError):self.store.complete_scene(ticket,'invalid-image')
        self.assertEqual(self.view()['version'],before)
