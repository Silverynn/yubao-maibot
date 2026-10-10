"""Lv2.5 语义与持久化测试；不调用真实模型，不使用既有房间。"""
from pathlib import Path
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import AsyncMock
import json
import sqlite3
import tempfile
import unittest

import test_storyroom as baseline
from storyroom.store import RoomStore

worker_module = baseline.worker_module


class StoryTests(unittest.TestCase):
    setUp = baseline.PlatformTests.setUp
    snapshot = baseline.PlatformTests.snapshot
    request_task = baseline.PlatformTests.request_task
    propose = baseline.PlatformTests.propose

    def post(self, suffix, data):
        return self.client.post(self.prefix + suffix, headers=self.host, json=data)

    def confirm(self, task, **fields):
        return self.post('/tasks/' + task['id'] + '/decide', {'ruling': '调查失败，但学会与同伴合作', **fields})

    def test_existing_room_script_frozen_when_template_changes(self):
        old = self.snapshot()
        self.client.app.state.store.stories['lighthouse']['world'] = '外星新主线'
        self.assertEqual(self.snapshot()['world'], old['world'])
        self.assertEqual(self.snapshot()['script_hash'], old['script_hash'])

    def test_card_skills_and_goal_enter_only_story_context(self):
        card = {'background': '在港口修过船', 'goal': '帮助渔船回家', 'skills': {'craft': 3}}
        self.assertEqual(self.post('/roles/' + self.players[0]['id'] + '/card', card).status_code, 200)
        task = self.request_task()
        self.assertEqual(task['card']['skills']['craft'], 3)
        self.assertEqual(task['card']['goal'], card['goal'])
        self.assertEqual(self.post('/roles/' + self.players[0]['id'] + '/card', card).status_code, 409)

    def test_invalid_skill_and_model_mood_mutation_rejected(self):
        self.assertEqual(self.post('/roles/' + self.players[0]['id'] + '/card', {'skills': {'craft': 99}}).status_code, 422)
        task = self.request_task()
        self.assertEqual(self.client.post('/api/bot/tasks/' + task['id'] + '/result', headers=self.pc1,
            json={'lease': task['lease'], 'speech': '成功', 'action': '我提升心情', 'mood_delta': 10}).status_code, 422)

    def test_unconfirmed_proposal_is_not_memory_or_history(self):
        task = self.request_task()
        self.propose(task)
        # 0.3串行发言：未裁决行动阻止同伴领取，取消后检查提案不变成事实。
        self.post('/tasks/' + task['id'] + '/cancel', {})
        other = self.request_task(1)
        self.assertEqual(other['memories'], [])
        self.assertNotIn('搜索桌子', json.dumps(other['history'], ensure_ascii=False))

    def test_confirmed_failure_changes_mood_and_future_memory(self):
        task = self.request_task()
        self.propose(task)
        self.assertEqual(self.confirm(task, mood_delta=-10, trust_delta=1).status_code, 200)
        state = self.snapshot()
        self.assertEqual(state['roles'][0]['mood'], 40)
        self.assertEqual(state['flags'], [])
        self.assertEqual(len(state['experiences']), 1)
        following = self.request_task()
        self.assertEqual(following['mood'], 40)
        self.assertEqual(following['trust'], 1)
        self.assertIn('调查失败', following['memories'][0]['text'])

    def test_rejected_action_has_no_persistent_consequence(self):
        task = self.request_task()
        self.propose(task)
        self.confirm(task, approve=False, mood_delta=10, trust_delta=2)
        state = self.snapshot()
        self.assertEqual(state['roles'][0]['mood'], 50)
        self.assertEqual(state['roles'][0]['trust'], 0)
        self.assertEqual(state['experiences'], [])

    def test_revoked_experience_not_reintroduced_through_event_history(self):
        task = self.request_task()
        self.propose(task)
        self.confirm(task, ruling='需要撤销的独特经历')
        memory = self.snapshot()['experiences'][0]
        self.post('/experiences/' + memory['id'], {'text': memory['text'], 'active': False})
        following = self.request_task()
        self.assertEqual(following['memories'], [])
        self.assertNotIn('需要撤销的独特经历', json.dumps(following, ensure_ascii=False))

    def test_revised_experience_replaces_old_prompt_but_keeps_audit(self):
        task = self.request_task()
        self.propose(task)
        self.confirm(task, ruling='错误的独特经历')
        memory = self.snapshot()['experiences'][0]
        self.post('/experiences/' + memory['id'], {'text': '修正的独特经历'})
        following = self.request_task()
        wire = json.dumps(following, ensure_ascii=False)
        self.assertIn('修正的独特经历', wire)
        self.assertNotIn('错误的独特经历', wire)
        self.assertTrue(any(e['kind'] == '经历修正或撤销' for e in self.snapshot()['events']))

    def test_check_uses_card_bonus_lock_difficulty_and_roll_once(self):
        self.post('/roles/' + self.players[0]['id'] + '/card', {'skills': {'craft': 3}})
        task = self.request_task()
        self.propose(task)
        self.assertEqual(self.post('/tasks/' + task['id'] + '/check', {'skill': 'craft', 'dc': 12}).status_code, 200)
        self.assertEqual(self.post('/tasks/' + task['id'] + '/check', {'skill': 'craft', 'dc': 1}).status_code, 409)
        self.assertEqual(self.confirm(task).status_code, 409)
        store = self.client.app.state.store
        calls = []
        store.roll = lambda sides: calls.append(sides) or 0
        result = self.post('/tasks/' + task['id'] + '/roll', {}).json()
        self.assertEqual(result['roll'], 1)
        self.assertEqual(result['modifier'], 3)
        self.assertEqual(result['total'], 4)
        self.assertEqual(result['success'], 0)
        self.assertTrue(self.post('/tasks/' + task['id'] + '/roll', {}).json()['duplicate'])
        self.assertEqual(calls, [20])
        self.assertEqual(self.snapshot()['flags'], [])
        self.assertEqual(self.confirm(task).status_code, 200)

    def test_check_cannot_roll_after_scene_changes(self):
        task = self.request_task()
        self.propose(task)
        self.post('/tasks/' + task['id'] + '/check', {'skill': 'investigate', 'dc': 12})
        self.post('/narrate', {'text': '门已关上'})
        self.assertEqual(self.post('/tasks/' + task['id'] + '/roll', {}).status_code, 409)

    def test_player_cannot_edit_card_experience_or_check(self):
        for suffix, data in (('/roles/' + self.players[0]['id'] + '/card', {}),
                             ('/experiences/unknown', {'text': '擅自改变过去'}),
                             ('/tasks/unknown/check', {'skill': 'social', 'dc': 1})):
            self.assertEqual(self.client.post(self.prefix + suffix, headers=self.pc1, json=data).status_code, 403)

    def test_audit_includes_context_output_plugin_state_without_credentials(self):
        task = self.request_task()
        self.propose(task)
        self.confirm(task)
        result = self.client.get(self.prefix + '/tasks/' + task['id'] + '/audit', headers=self.host).json()
        self.assertEqual(result['context']['mood'], 50)
        self.assertEqual(result['result']['speech'], '我试着找齿轮')
        self.assertTrue(any(e['detail'].get('插件开启') is True for e in result['events']))
        for sensitive in (task['lease'], self.players[0]['token'], 'lease_hash', 'key_hash'):
            self.assertNotIn(sensitive, json.dumps(result))
        self.assertEqual(self.client.get(self.prefix + '/tasks/' + task['id'] + '/audit', headers=self.pc1).status_code, 403)

    def test_demo_logs_do_not_claim_real_plugin_or_model_usage(self):
        self.post('/demo', {})
        for event in self.snapshot()['events']:
            if '插件开启' in event['detail']:
                self.assertFalse(event['detail']['插件开启'])

    def test_cohost_is_scoped_revocable_and_audited_by_name(self):
        invited = self.post('/hosts', {'name': '另一位GM'}).json()
        key = {'Authorization': 'Bearer ' + invited['token']}
        self.assertEqual(self.client.post(self.prefix + '/narrate', headers=key, json={'text': '共同主持'}).status_code, 200)
        self.assertEqual(self.snapshot()['events'][0]['actor'], '另一位GM')
        other = self.client.post('/api/rooms', headers=self.host, json={}).json()['room_id']
        self.assertEqual(self.client.get('/api/rooms/' + other, headers=key).status_code, 403)
        self.assertEqual(self.client.post('/api/rooms', headers=key, json={}).status_code, 403)
        self.assertEqual(self.client.post(self.prefix + '/hosts', headers=key, json={'name': '无限转授'}).status_code, 403)
        self.assertEqual(self.post('/hosts/' + invited['id'] + '/revoke', {}).status_code, 200)
        self.assertEqual(self.client.get(self.prefix, headers=key).status_code, 401)

    def test_plugin_off_receipt_records_state_without_claiming_connected(self):
        response = self.client.post('/api/bot/state', headers=self.pc1, json={'enabled': False})
        self.assertEqual(response.status_code, 200)
        event = self.snapshot()['events'][0]
        self.assertFalse(event['detail']['插件开启'])
        self.assertFalse(self.snapshot()['roles'][0]['connected'])

    def test_stage_receipts_do_not_push_confirmed_narrative_out_of_context(self):
        self.post('/narrate', {'text': '必须被看到的最新现场'})
        store = self.client.app.state.store
        with store.db() as db:
            for _ in range(170):
                store.event(db, self.room, 'Live2D舞台回执', '舞台网页', {'说明': '多位观众回执'})
        task = self.request_task()
        self.assertIn('必须被看到的最新现场', json.dumps(task['history'], ensure_ascii=False))

    def test_legacy_db_upgrade_keeps_credentials_and_creates_sqlite_backup(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with closing(sqlite3.connect(root / 'rooms.sqlite3')) as db:
                db.execute('CREATE TABLE rooms(id TEXT PRIMARY KEY,name TEXT,story TEXT,node TEXT,flags TEXT,version INTEGER,created REAL)')
                db.execute("INSERT INTO rooms VALUES('old','旧房间','lighthouse','workshop','[]',7,0)")
                db.commit()
            store = RoomStore(root, Path(__file__).resolve().parents[1] / 'stories')
            view = store.snapshot('old')
            self.assertEqual(view['node'], 'workshop')
            self.assertEqual(view['version'], 7)
            self.assertEqual(len(list((root / '备份').glob('*.sqlite3'))), 1)


class OutboxTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='storyroom-outbox-')
        self.addCleanup(self.temp.cleanup)
        self.task = {'id': 'task', 'lease': 'sensitive-lease', 'question': '怎么办', 'character_name': '测试角色',
                     'world': '有限港口', 'stage': {}, 'flags': [], 'trust': 0, 'history': []}
        self.generate = AsyncMock(return_value='{"speech":"先观察","action":"调查","emotion":"疑惑"}')
        self.audit = worker_module.Audit(Path(self.temp.name) / '日志')

    def worker(self, client):
        return worker_module.PCWorker(client, self.generate, '', '', 1, lambda text: None,
                                      data_dir=Path(self.temp.name), audit=self.audit)

    async def test_disconnect_then_process_restart_resends_without_model_call(self):
        client = SimpleNamespace(binding='same-server-role', request=AsyncMock(side_effect=[{'task': self.task}, OSError('断网')]))
        with self.assertRaises(OSError):
            await self.worker(client).once()
        restored = self.worker(SimpleNamespace(binding='same-server-role', request=AsyncMock(return_value={'ok': True})))
        self.assertTrue(await restored.once())
        self.assertEqual(self.generate.await_count, 1)
        self.assertFalse((Path(self.temp.name) / 'pending-result.json').exists())
        text = ''.join(p.read_text(encoding='utf-8') for p in (Path(self.temp.name) / '日志').glob('*.jsonl'))
        self.assertIn('先观察', text)
        self.assertNotIn('sensitive-lease', text)

    async def test_changed_connection_never_receives_previous_role_result(self):
        client = SimpleNamespace(binding='old', request=AsyncMock(side_effect=[{'task': self.task}, OSError()]))
        with self.assertRaises(OSError):
            await self.worker(client).once()
        new = SimpleNamespace(binding='new', request=AsyncMock())
        self.assertFalse(await self.worker(new).once())
        new.request.assert_not_called()
        self.assertEqual(self.generate.await_count, 1)

    async def test_outdated_lease_discards_result_without_regeneration(self):
        client = SimpleNamespace(binding='same', request=AsyncMock(side_effect=[{'task': self.task}, worker_module.PlatformError(409)]))
        self.assertFalse(await self.worker(client).once())
        self.assertEqual(self.generate.await_count, 1)
        self.assertFalse((Path(self.temp.name) / 'pending-result.json').exists())

    async def test_protocol_mismatch_stops_before_model(self):
        client = worker_module.RoomClient('http://127.0.0.1:8790', 'not-a-real-secret')
        client.request = AsyncMock(return_value={'service': 'storyroom', 'protocol': '0.1'})
        with self.assertRaises(ValueError):
            await self.worker(client).run()
        self.generate.assert_not_called()


if __name__ == '__main__':
    unittest.main()
