"""Transactional runtime controls and immutable story branches.

Credentials and connection leases are deliberately excluded from checkpoints.
Legacy rooms keep their previous behaviour until runtime controls are enabled.
"""
import json
import uuid
from .store_types import fail


STATE_TABLES = ('tasks', 'experiences', 'room_notes', 'participants', 'fu_cards', 'fu_checks', 'scenes')
ROOM_FIELDS = ('name', 'story', 'node', 'flags', 'script', 'script_hash')
ROLE_FIELDS = ('id', 'name', 'avatar', 'visual', 'trust', 'card', 'mood')
AUDIT_KINDS = {'Live2D舞台回执', '跑团插件开关', 'StoryRoom语音与实时状态', 'StoryRoom语音识别', '本次上下文与回忆', '玩家开始思考', '主持人模型用量'}


class RuntimeFeatures:
    def init_runtime(self, db):
        db.executescript('''
        CREATE TABLE IF NOT EXISTS runtime(room TEXT PRIMARY KEY,enabled INTEGER DEFAULT 0,
          state TEXT DEFAULT 'PAUSED',gm_mode TEXT DEFAULT 'HUMAN',controller TEXT DEFAULT '',
          controller_until REAL DEFAULT 0,branch TEXT,epoch INTEGER DEFAULT 1,revision INTEGER DEFAULT 1,
          gm_epoch INTEGER DEFAULT 1,gm_lease TEXT DEFAULT '',gm_until REAL DEFAULT 0,
          gm_status TEXT DEFAULT '未配置',gm_calls INTEGER DEFAULT 0,gm_budget INTEGER DEFAULT 100,
          gm_context TEXT DEFAULT '',error TEXT DEFAULT '',ruleset TEXT DEFAULT 'simple');
        CREATE TABLE IF NOT EXISTS branches(id TEXT PRIMARY KEY,room TEXT,parent TEXT,
          fork_checkpoint TEXT,created REAL,label TEXT);
        CREATE TABLE IF NOT EXISTS event_branches(branch TEXT,seq INTEGER,PRIMARY KEY(branch,seq));
        CREATE TABLE IF NOT EXISTS checkpoints(id TEXT PRIMARY KEY,room TEXT,branch TEXT,
          seq INTEGER,created REAL,label TEXT,payload TEXT);
        CREATE VIEW IF NOT EXISTS active_events AS SELECT e.* FROM events e
          JOIN runtime r ON r.room=e.room JOIN event_branches b ON b.seq=e.seq AND b.branch=r.branch;
        CREATE TABLE IF NOT EXISTS fu_cards(room TEXT,role TEXT PRIMARY KEY,data TEXT);
        CREATE TABLE IF NOT EXISTS fu_checks(room TEXT,task TEXT PRIMARY KEY,data TEXT);
        ''')
        for column, definition in [('pc_calls','INTEGER DEFAULT 0'),('pc_budget','INTEGER DEFAULT 200'),('image_calls','INTEGER DEFAULT 0')]:
            if column not in {r['name'] for r in db.execute('PRAGMA table_info(runtime)')}:
                db.execute('ALTER TABLE runtime ADD COLUMN '+column+' '+definition)
        db.execute("CREATE TABLE IF NOT EXISTS scenes(room TEXT PRIMARY KEY,branch TEXT,node TEXT,revision TEXT,prompt TEXT,image TEXT,status TEXT,job TEXT,hash TEXT,error TEXT)")
        db.execute("UPDATE scenes SET status='queued',job='' WHERE status='generating'")
        for row in db.execute('SELECT id FROM rooms').fetchall():
            self.ensure_runtime(db, row['id'])
        # Restarts never resume chargeable work automatically.
        db.execute("UPDATE tasks SET status='queued',lease_hash=NULL,lease_until=0 WHERE status='running' AND room IN (SELECT room FROM runtime WHERE enabled=1)")
        db.execute("UPDATE runtime SET state='PAUSED',epoch=epoch+1,revision=revision+1,gm_lease='',gm_until=0,controller_until=0 WHERE enabled=1")
        for row in db.execute('SELECT room FROM runtime').fetchall():
            if not db.execute('SELECT 1 FROM checkpoints WHERE room=?', (row['room'],)).fetchone():
                self.checkpoint(db, row['room'], '升级时的完整状态')

    def ensure_runtime(self, db, room):
        if not db.execute('SELECT 1 FROM runtime WHERE room=?', (room,)).fetchone():
            branch = uuid.uuid4().hex
            db.execute('INSERT INTO branches VALUES(?,?,NULL,NULL,?,?)', (branch, room, self.clock(), '原始故事'))
            db.execute('INSERT INTO runtime(room,branch) VALUES(?,?)', (room, branch))
            db.execute('INSERT OR IGNORE INTO event_branches SELECT ?,seq FROM events WHERE room=?', (branch, room))
        return dict(db.execute('SELECT * FROM runtime WHERE room=?', (room,)).fetchone())

    def runtime_view(self, db, room):
        value = self.ensure_runtime(db, room)
        return {**{k: v for k, v in value.items() if k not in ('gm_lease', 'gm_context')}, "gm_configured": bool(getattr(self,"gm_configured",False))}

    def progress_guard(self, db, room, manual=False):
        from .store import actor_name, actor_id
        runtime = self.ensure_runtime(db, room)
        if not runtime['enabled']:
            return
        if runtime['state'] != 'RUNNING':
            fail('整场游戏已暂停；恢复后才能推进剧情', 409)
        if manual and actor_id.get() != 'internal-gm':
            if runtime['gm_mode'] != 'HUMAN':
                fail('请先接管主持人，再发布叙事或裁决', 409)
            if runtime['controller_until'] <= self.clock() or runtime['controller'] != actor_id.get():
                fail('请先获取主持权；只有当前主持者可以裁决', 409)

    def capture(self, db, room):
        state = self.room(db, room)
        self.sync_scene(db,room)
        payload = {'room': {k: state[k] for k in ROOM_FIELDS}, 'roles': [
            {k: r[k] for k in ROLE_FIELDS} for r in db.execute('SELECT * FROM roles WHERE room=? ORDER BY rowid', (room,))],
            'events': [r[0] for r in db.execute('SELECT seq FROM active_events WHERE room=? ORDER BY seq', (room,))],
            'talk': dict(db.execute('SELECT * FROM room_talk WHERE room=?', (room,)).fetchone()),
            'runtime': {k: v for k, v in self.ensure_runtime(db, room).items() if k in ('gm_mode', 'ruleset')}}
        for table in STATE_TABLES:
            payload[table] = [dict(r) for r in db.execute('SELECT * FROM '+table+' WHERE room=?', (room,))]
        payload['checks'] = [dict(r) for r in db.execute('SELECT c.* FROM checks c JOIN tasks t ON t.id=c.task WHERE t.room=?', (room,))]
        for check in payload['fu_checks']:
            data=json.loads(check['data'])
            data.update(choice_lease='',choice_until=0)
            check['data']=json.dumps(data,ensure_ascii=False)
        for task in payload['tasks']:
            task['lease_hash'], task['lease_until'], task['context'] = None, 0, None
        return payload

    def checkpoint(self, db, room, label='完整事件之后'):
        data = self.capture(db, room)
        runtime = self.ensure_runtime(db, room)
        seq = max(data['events'], default=0)
        identifier = uuid.uuid4().hex
        db.execute('INSERT INTO checkpoints VALUES(?,?,?,?,?,?,?)', (identifier, room, runtime['branch'], seq, self.clock(), label[:100], json.dumps(data, ensure_ascii=False)))
        return identifier

    def checkpoint_changes(self, db, before):
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='runtime'").fetchone():
            return
        changed = db.execute('SELECT room,kind FROM events WHERE seq>? ORDER BY seq', (before,)).fetchall()
        rooms = {r['room']: r['kind'] for r in changed if r['kind'] not in AUDIT_KINDS}
        for room, label in rooms.items():
            self.checkpoint(db, room, label)

    def control_runtime(self, room, operation, revision, actor):
        with self.db() as db:
            self.room(db, room)
            value = self.ensure_runtime(db, room)
            if value['revision'] != revision:
                fail('控制状态已变化，请同步后重试', 409)
            self.checkpoint(db, room, '控制操作之前')
            db.execute('UPDATE runtime SET enabled=1,revision=revision+1 WHERE room=?', (room,))
            if operation == 'pause':
                db.execute("UPDATE runtime SET state='PAUSED',epoch=epoch+1,gm_lease='',gm_until=0 WHERE room=?", (room,))
                db.execute("UPDATE tasks SET status='queued',lease_hash=NULL,lease_until=0,queued_at=? WHERE room=? AND status='running'", (self.clock(), room))
                db.execute('UPDATE room_talk SET opened=0,host_until=0,audio_until=0 WHERE room=?', (room,))
            elif operation == 'resume':
                if value['gm_mode'] == 'HUMAN' and (value['controller'] != actor or value['controller_until'] <= self.clock()):
                    fail('请先接管主持人，再恢复游戏', 409)
                if value['gm_mode'] == 'AI' and not getattr(self, 'gm_configured', False):
                    fail('尚未配置 AI 主持人模型；可先用人类主持模式', 409)
                db.execute("UPDATE runtime SET state='RUNNING',error='' WHERE room=?", (room,))
                db.execute("UPDATE tasks SET queued_at=? WHERE room=? AND status='queued'", (self.clock(), room))
                db.execute('UPDATE room_talk SET opened=1,remaining=limit_turns,ready_at=0 WHERE room=?', (room,))
            elif operation == 'takeover':
                if value['gm_mode'] == 'HUMAN' and value['controller'] and value['controller'] != actor and value['controller_until'] > self.clock():
                    fail('另一位主持人持有主持权，请先让其释放', 409)
                db.execute("UPDATE runtime SET gm_mode='HUMAN',controller=?,controller_until=?,gm_epoch=gm_epoch+1,gm_lease='',gm_until=0,gm_status='人工主持' WHERE room=?", (actor, self.clock()+90, room))
                db.execute('UPDATE room_talk SET host_until=0,audio_until=0 WHERE room=?', (room,))
            elif operation in ('handoff', 'release'):
                if value['controller'] != actor or value['controller_until'] <= self.clock():
                    fail('只有当前主持者可以移交或释放主持权', 409)
                if operation == 'handoff' and not getattr(self, 'gm_configured', False):
                    fail('尚未配置 AI 主持人模型', 409)
                db.execute("UPDATE runtime SET gm_mode=?,controller='',controller_until=0,gm_epoch=gm_epoch+1,gm_lease='',gm_until=0,gm_status=? WHERE room=?", ('AI' if operation=='handoff' else 'HUMAN', '等待任务' if operation=='handoff' else '等待人类接管', room))
                if operation == 'release':
                    db.execute("UPDATE runtime SET state='PAUSED',epoch=epoch+1 WHERE room=?", (room,))
                    db.execute("UPDATE tasks SET status='queued',lease_hash=NULL,lease_until=0,queued_at=? WHERE room=? AND status='running'",(self.clock(),room))
                    db.execute('UPDATE room_talk SET opened=0,host_until=0,audio_until=0 WHERE room=?',(room,))
            else:
                fail('未知控制操作')
            self.event(db, room, '房间运行控制', '主持人', {'操作': operation, '主持模式': self.ensure_runtime(db, room)['gm_mode']})
        return {'ok': True}

    def renew_controller(self, room, actor):
        with self.db() as db:
            value = self.ensure_runtime(db, room)
            if value['gm_mode'] != 'HUMAN' or value['controller'] != actor or value['controller_until'] <= self.clock():
                fail('主持权不存在或已过期，请重新接管', 409)
            db.execute('UPDATE runtime SET controller_until=? WHERE room=?', (self.clock()+90, room))
        return {'ok': True}

    def timeline(self, room):
        with self.db() as db:
            self.room(db, room)
            runtime = self.runtime_view(db, room)
            checkpoints = [dict(r) for r in db.execute('SELECT id,branch,seq,created,label FROM checkpoints WHERE room=? ORDER BY created DESC,rowid DESC LIMIT 200', (room,))]
            branches = [dict(r) for r in db.execute('SELECT * FROM branches WHERE room=? ORDER BY created', (room,))]
            return {'runtime': runtime, 'checkpoints': checkpoints, 'branches': branches}

    def rewind_preview(self, room, checkpoint):
        with self.db() as db:
            row = db.execute('SELECT * FROM checkpoints WHERE id=? AND room=?', (checkpoint, room)).fetchone()
            if not row:
                fail('检查点不存在或不属于此房间', 404)
            payload = json.loads(row['payload'])
            return {'id': row['id'], 'branch': row['branch'], 'label': row['label'], 'seq': row['seq'],
                    'room': payload['room'], 'roles': payload['roles'], 'rules': payload['fu_cards'],
                    'notes': payload['room_notes'], 'experiences': payload['experiences'],
                    'warning': '旧未来保留为归档分支。API费用、已导出的文件与人类已见信息无法撤销。'}

    def rewind(self, room, checkpoint, revision, actor):
        with self.db() as db:
            state = self.room(db, room)
            value = self.ensure_runtime(db, room)
            if not value['enabled'] or value['state'] != 'PAUSED' or value['revision'] != revision:
                fail('请先暂停并同步最新控制版本，再回溯', 409)
            row = db.execute('SELECT * FROM checkpoints WHERE id=? AND room=?', (checkpoint, room)).fetchone()
            if not row:
                fail('检查点不存在或不属于此房间', 404)
            payload = json.loads(row['payload'])
            self.checkpoint(db, room, '回溯前的归档现场')
            branch = uuid.uuid4().hex
            db.execute('INSERT INTO branches VALUES(?,?,?,?,?,?)', (branch, room, row['branch'], checkpoint, self.clock(), '回溯：'+row['label']))
            db.executemany('INSERT INTO event_branches VALUES(?,?)', [(branch, seq) for seq in payload['events']])
            db.execute('DELETE FROM checks WHERE task IN (SELECT id FROM tasks WHERE room=?)', (room,))
            for table in STATE_TABLES:
                db.execute('DELETE FROM '+table+' WHERE room=?', (room,))
                rows = payload[table]
                for item in rows:
                    if table == 'scenes':
                        item['branch']=branch
                        item['job']=''
                        if item['status']=='generating': item['status']='queued'
                    if table == 'tasks':
                        item['version'] = state['version']+1
                        item['lease_hash'], item['lease_until'], item['context'] = None, 0, None
                        if item['status'] in ('queued', 'running'):
                            item['status'] = 'cancelled'
                    columns = list(item)
                    db.execute('INSERT INTO '+table+'('+','.join(columns)+') VALUES('+','.join('?' for _ in columns)+')', [item[k] for k in columns])
            for item in payload['checks']:
                columns = list(item)
                db.execute('INSERT INTO checks('+','.join(columns)+') VALUES('+','.join('?' for _ in columns)+')', [item[k] for k in columns])
            restored = payload['room']
            db.execute('UPDATE rooms SET '+','.join(k+'=?' for k in ROOM_FIELDS)+',version=? WHERE id=?', [restored[k] for k in ROOM_FIELDS]+[state['version']+1,room])
            for person in payload['roles']:
                person['visual'] = json.dumps({'emotion':'平静','revision':uuid.uuid4().hex})
                fields = [k for k in ROLE_FIELDS if k != 'id']
                db.execute('UPDATE roles SET '+','.join(k+'=?' for k in fields)+' WHERE id=? AND room=?', [person[k] for k in fields]+[person['id'],room])
            talk = payload['talk']
            db.execute('UPDATE room_talk SET opened=0,remaining=?,next_role=?,limit_turns=?,host_until=0,audio_until=0,ready_at=0 WHERE room=?', (talk['remaining'],talk['next_role'],talk['limit_turns'],room))
            db.execute("UPDATE runtime SET enabled=1,state='PAUSED',branch=?,epoch=epoch+1,revision=revision+1,gm_epoch=gm_epoch+1,gm_lease='',gm_until=0,gm_context='',controller='',controller_until=0,gm_mode=?,ruleset=?,error='' WHERE room=?", (branch,payload['runtime']['gm_mode'],payload['runtime']['ruleset'],room))
            self.event(db, room, '时间回溯', '主持人', {'检查点':checkpoint,'旧分支':value['branch'],'新分支':branch,'说明':'已恢复完整现场；暂停等待确认后继续'})
        return {'ok':True,'branch':branch}

    def script_export(self, room, branch=None):
        from .room_features import PUBLIC_KINDS
        with self.db() as db:
            info = self.room(db, room)
            branch = branch or self.ensure_runtime(db, room)['branch']
            if not db.execute('SELECT 1 FROM branches WHERE id=? AND room=?', (branch,room)).fetchone():
                fail('分支不属于此房间',404)
            rows = db.execute('SELECT e.* FROM events e JOIN event_branches b ON b.seq=e.seq WHERE e.room=? AND b.branch=? ORDER BY e.seq', (room,branch)).fetchall()
            lines = ['# '+info['name'], '', '剧情分支：'+branch, '']
            for row in rows:
                if row['kind'] not in PUBLIC_KINDS + ('最终物语检定','规则资源变化','时间回溯','房间运行控制'):
                    continue
                detail = json.loads(row['detail'])
                text = detail.get('发言', detail.get('内容', detail.get('裁决', '')))
                if not text:
                    text = json.dumps(detail, ensure_ascii=False)
                lines += [f"【{row['actor']}】{text}", f"<!-- event:{row['seq']} {row['at']} {row['kind']} -->", '']
            return '\n'.join(lines)
