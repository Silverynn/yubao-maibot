"""复用房间SQLite：剧本编辑、隔离记忆、公共观察及轮流讨论。"""
import json
import uuid


PUBLIC_KINDS = ('世界线开始', '主持人叙事', '世界线推进', '主持人裁决',
                '玩家提交提案', '玩家讨论', '玩家提问', '玩家交流', '角色卡更新',
                '跑团记忆更新', '跑团玩家资料更新', '剧本应用', '讨论控制', '主持人发言状态', '发言状态失联')


class RoomFeatures:
    def init_features(self, db):
        from .store import dump, digest
        db.executescript('''
        CREATE TABLE IF NOT EXISTS story_templates(id TEXT PRIMARY KEY,script TEXT,hash TEXT);
        CREATE TABLE IF NOT EXISTS room_notes(id TEXT PRIMARY KEY,room TEXT,subject TEXT,text TEXT,active INTEGER,updated REAL);
        CREATE TABLE IF NOT EXISTS participants(id TEXT PRIMARY KEY,room TEXT,name TEXT,profile TEXT,active INTEGER);
        CREATE TABLE IF NOT EXISTS room_talk(room TEXT PRIMARY KEY,opened INTEGER DEFAULT 0,remaining INTEGER DEFAULT 0,
          limit_turns INTEGER DEFAULT 4,next_role INTEGER DEFAULT 0,host_client TEXT DEFAULT '',host_until REAL DEFAULT 0,
          audio_client TEXT DEFAULT '',audio_task TEXT DEFAULT '',audio_until REAL DEFAULT 0,ready_at REAL DEFAULT 0);
        ''')
        for identifier, script in self.stories.items():
            db.execute('INSERT OR IGNORE INTO story_templates VALUES(?,?,?)', (identifier, dump(script), digest(dump(script))))
        db.execute('INSERT OR IGNORE INTO room_talk(room) SELECT id FROM rooms')
        if 'autonomous' not in {r['name'] for r in db.execute('PRAGMA table_info(tasks)')}:
            db.execute('ALTER TABLE tasks ADD COLUMN autonomous INTEGER DEFAULT 0')
        if 'event_seq' not in {r['name'] for r in db.execute('PRAGMA table_info(room_notes)')}:
            db.execute('ALTER TABLE room_notes ADD COLUMN event_seq INTEGER DEFAULT 0')
        if 'queued_at' not in {r['name'] for r in db.execute('PRAGMA table_info(tasks)')}:
            db.execute('ALTER TABLE tasks ADD COLUMN queued_at REAL DEFAULT 0')
            db.execute("UPDATE tasks SET queued_at=? WHERE status='queued'", (self.clock(),))

    def templates(self):
        with self.db() as db:
            return [{'id': r['id'], 'hash': r['hash'], 'script': json.loads(r['script'])}
                    for r in db.execute('SELECT * FROM story_templates ORDER BY id')]

    def save_template(self, script, expected_hash):
        from .authoring import validate_story
        from .store import RoomError, dump, digest
        script = validate_story(script)
        raw = dump(script)
        with self.db() as db:
            old = db.execute('SELECT hash FROM story_templates WHERE id=?', (script['id'],)).fetchone()
            if (old['hash'] if old else '') != expected_hash:
                raise RoomError('模板已经被修改，请重新载入；新模板应使用新ID', 409)
            db.execute('INSERT OR REPLACE INTO story_templates VALUES(?,?,?)', (script['id'], raw, digest(raw)))
        return {'id': script['id'], 'hash': digest(raw), 'script': script}

    def apply_script(self, room, script, expected_hash):
        from .authoring import validate_story
        from .store import RoomError, dump, digest
        script = validate_story(script)
        raw = dump(script)
        with self.db() as db:
            state = self.room(db, room)
            if state['script_hash'] != expected_hash:
                raise RoomError('房间剧本已变化，请重新载入', 409)
            if state['node'] not in script['nodes'] or not set(json.loads(state['flags'])).issubset(script['flags']):
                raise RoomError('新剧本不能删除当前阶段或已经确认的标记；可另建房间', 409)
            if db.execute("SELECT 1 FROM tasks WHERE room=? AND status IN ('queued','running','proposed')", (room,)).fetchone():
                raise RoomError('请先裁决或取消待处理任务，再应用剧本', 409)
            db.execute('UPDATE rooms SET script=?,script_hash=?,story=?,version=version+1 WHERE id=?',
                       (raw, digest(raw), script['id'], room))
            db.execute('UPDATE room_talk SET opened=0,remaining=0 WHERE room=?', (room,))
            self.event(db, room, '剧本应用', '主持人', {'旧版本': state['script_hash'], '新版本': digest(raw), '说明': '显式应用；保留当前阶段、事实、记忆和历史'})
        self.export(room)
        return {'hash': digest(raw)}

    def memory_view(self, db, room):
        players = [dict(r) for r in db.execute('SELECT id,name,card FROM roles WHERE room=?', (room,))]
        for r in players:
            r['card'] = json.loads(r['card'])
        people = [dict(r) for r in db.execute('SELECT id,name,profile FROM participants WHERE room=? AND active=1', (room,))]
        notes = [dict(r) for r in db.execute('SELECT id,subject,text FROM room_notes WHERE room=? AND active=1 ORDER BY updated DESC', (room,))]
        return {'namespace': 'storyroom:' + room + ':' + self.ensure_runtime(db, room)['branch'], 'players': players, 'other_players': people, 'notes': notes}

    def set_note(self, room, identifier, subject, text, active):
        from .store import RoomError
        with self.db() as db:
            self.room(db, room)
            old = db.execute('SELECT room FROM room_notes WHERE id=?', (identifier,)).fetchone() if identifier else None
            if identifier and (not old or old['room'] != room):
                raise RoomError('记忆不属于本房间', 404)
            if not identifier and db.execute('SELECT count(*) FROM room_notes WHERE room=?', (room,)).fetchone()[0] >= 500:
                raise RoomError('房间记忆上限500条，请修改已有条目', 409)
            identifier = identifier or uuid.uuid4().hex
            source = db.execute('SELECT event_seq FROM room_notes WHERE id=?', (identifier,)).fetchone()
            db.execute('INSERT OR REPLACE INTO room_notes(id,room,subject,text,active,updated,event_seq) VALUES(?,?,?,?,?,?,?)',
                       (identifier, room, subject, text, int(active), self.clock(), source['event_seq'] if source else 0))
            db.execute('UPDATE rooms SET version=version+1 WHERE id=?', (room,))
            db.execute("UPDATE tasks SET status='cancelled' WHERE room=? AND status IN ('queued','running')", (room,))
            self.event(db, room, '跑团记忆更新', '主持人', {'记忆': identifier, '主题': subject, '内容': text, '可回忆': active, '说明': '只进入本房间；撤销后不进入未来提示词，旧审计保留'})
        self.export(room)
        return {'id': identifier}

    def set_participant(self, room, identifier, name, profile, active):
        from .store import RoomError
        with self.db() as db:
            self.room(db, room)
            old = db.execute('SELECT room FROM participants WHERE id=?', (identifier,)).fetchone() if identifier else None
            if identifier and (not old or old['room'] != room):
                raise RoomError('玩家资料不属于本房间', 404)
            if not identifier and db.execute('SELECT count(*) FROM participants WHERE room=?', (room,)).fetchone()[0] >= 30:
                raise RoomError('额外玩家资料上限30人', 409)
            identifier = identifier or uuid.uuid4().hex
            db.execute('INSERT OR REPLACE INTO participants VALUES(?,?,?,?,?)', (identifier, room, name, profile, int(active)))
            db.execute('UPDATE rooms SET version=version+1 WHERE id=?', (room,))
            db.execute("UPDATE tasks SET status='cancelled' WHERE room=? AND status IN ('queued','running')", (room,))
            self.event(db, room, '跑团玩家资料更新', '主持人', {'玩家': identifier, '姓名': name, '资料': profile, '可回忆': active})
        self.export(room)
        return {'id': identifier}

    def dialogue(self, room, actor, text):
        with self.db() as db:
            self.progress_guard(db, room, manual=False)
            self.room(db, room)
            self.event(db, room, '玩家交流', actor, {'发言': text, '说明': '听到了这句话，不等于其描述的行动已发生'})
        self.export(room)
        return {'ok': True}

    def remember_narration(self, db, room, seq, text):
        db.execute('INSERT INTO room_notes(id,room,subject,text,active,updated,event_seq) VALUES(?,?,?,?,1,?,?)',
                   (uuid.uuid4().hex, room, '已确认故事', text, self.clock(), seq))

    def talk_view(self, db, room):
        db.execute('INSERT OR IGNORE INTO room_talk(room) VALUES(?)', (room,))
        talk = dict(db.execute('SELECT * FROM room_talk WHERE room=?', (room,)).fetchone())
        expired = []
        for field, label in (('host_until', '主持人'), ('audio_until', '机器人朗读')):
            if 0 < talk[field] <= self.clock():
                expired.append(label)
                db.execute('UPDATE room_talk SET ' + field + '=0,opened=0 WHERE room=?', (room,))
                talk[field] = 0
                talk['opened'] = 0
        if expired:
            self.event(db, room, '发言状态失联', '平台', {'发言者': expired, '说明': '租约到期不等于已经讲完；暂停自主讨论，请主持人确认现场后再继续'})
        talk['host_speaking'] = talk['host_until'] > self.clock()
        talk['audio_playing'] = talk['audio_until'] > self.clock()
        return talk

    def control_talk(self, room, opened, limit):
        with self.db() as db:
            state = self.room(db, room)
            from .store import RoomError
            if opened and self.story(state)['nodes'][state['node']].get('ending'):
                raise RoomError('故事已结束，不再自动讨论', 409)
            self.talk_view(db, room)
            db.execute('UPDATE room_talk SET opened=?,remaining=?,limit_turns=? WHERE room=?', (int(opened), limit if opened else 0, limit, room))
            if not opened:
                db.execute("UPDATE tasks SET status='cancelled' WHERE room=? AND autonomous=1 AND status='queued'", (room,))
            self.event(db, room, '讨论控制', '主持人', {'开放讨论': opened, '本轮最多发言': limit, '说明': '轮流讨论；提问或行动后等待主持人；仅同步不调用模型'})
        self.export(room)
        return {'ok': True}

    def host_speech(self, room, client, speaking):
        from .store import RoomError
        with self.db() as db:
            self.room(db, room)
            talk = self.talk_view(db, room)
            if talk['host_speaking'] and talk['host_client'] != client:
                raise RoomError('另一位主持人正在发言；请由该浏览器结束发言或等待租约到期', 409)
            changed = talk['host_speaking'] != speaking
            ready = self.clock() + 1
            if not speaking and talk['host_speaking']:
                latest = db.execute("SELECT result FROM tasks WHERE room=? AND status='discussed' ORDER BY rowid DESC LIMIT 1", (room,)).fetchone()
                if latest:
                    ready = self.clock() + max(3, min(15, len(json.loads(latest['result']).get('speech', '')) / 8))
            db.execute('UPDATE room_talk SET host_client=?,host_until=?,audio_until=0,ready_at=? WHERE room=?',
                       (client if speaking else '', self.clock() + 90 if speaking else 0, ready, room))
            if changed:
                self.event(db, room, '主持人发言状态', '主持人', {'正在发言': speaking, '说明': '发言期间不领取新任务；已生成内容暂存，停止浏览器朗读'})
        if changed:
            self.export(room)
        return {'ok': True}

    def audio_lease(self, room, client, task, playing):
        from .store import RoomError
        with self.db() as db:
            talk = self.talk_view(db, room)
            if not playing:
                if talk['audio_client'] == client and talk['audio_task'] == task:
                    db.execute('UPDATE room_talk SET audio_until=0,ready_at=? WHERE room=?', (self.clock() + 1, room))
                return {'ok': True}
            self.progress_guard(db, room)
            row = db.execute('SELECT status,version,result FROM tasks WHERE id=? AND room=?', (task, room)).fetchone()
            if not row or row['status'] not in ('discussed', 'proposed', 'approved') or row['version'] != self.room(db, room)['version']:
                raise RoomError('此发言已失效', 409)
            if talk['host_speaking'] or (talk['audio_playing'] and (talk['audio_client'], talk['audio_task']) != (client, task)):
                raise RoomError('房间已有发言者，请等待', 409)
            db.execute('UPDATE room_talk SET audio_client=?,audio_task=?,audio_until=? WHERE room=?', (client, task, self.clock() + 45, room))
        return {'ok': True}

    def schedule(self, db, room):
        """没有人讲话、没有待主持人回应的任务时，才邀请下一位自行思考。"""
        runtime = self.ensure_runtime(db, room)
        if runtime["enabled"] and runtime["state"] != "RUNNING":
            return
        if runtime["enabled"] and runtime["gm_mode"] == "HUMAN" and runtime["controller_until"] <= self.clock():
            return
        talk = self.talk_view(db, room)
        if (not talk['opened'] or talk['remaining'] <= 0 or talk['host_speaking'] or talk['audio_playing']
                or talk['ready_at'] > self.clock()):
            return
        if db.execute("SELECT 1 FROM tasks WHERE room=? AND status IN ('queued','running','proposed')", (room,)).fetchone():
            return
        state = self.room(db, room)
        if self.story(state)['nodes'][state['node']].get('ending'):
            return
        roles = db.execute('SELECT id,name FROM roles WHERE room=? ORDER BY rowid', (room,)).fetchall()
        role = roles[talk['next_role'] % len(roles)]
        task = uuid.uuid4().hex
        question = '主持人已介绍完现场。结合已确认故事、其他玩家的话和你的角色目标，自己思考当前困境。你可以与同伴讨论、向主持人提问，或提出希望获得某个结果的行动。不要重复已有发言；不宣布行动成功。'
        db.execute("INSERT INTO tasks(id,room,role,node,version,question,status,autonomous,queued_at) VALUES(?,?,?,?,?,?,'queued',1,?)",
                   (task, room, role['id'], state['node'], state['version'], question, self.clock()))
        db.execute('UPDATE room_talk SET remaining=remaining-1,next_role=next_role+1 WHERE room=?', (room,))
        self.event(db, room, '自主讨论排队', '平台', {'任务': task, '玩家': role['name'], '剩余发言': talk['remaining'] - 1})

    def observe(self, owner, after):
        """每次最多扫描64条并按游标补齐，旁听不调用模型，不传其他人的私有上下文。"""
        from .store import dump
        room = owner['room']
        with self.db() as db:
            state = self.room(db, room)
            db.execute('UPDATE roles SET last_seen=? WHERE id=?', (self.clock(), owner['id']))
            rows = db.execute('SELECT seq,at,kind,actor,detail FROM active_events WHERE room=? AND seq>? ORDER BY seq LIMIT 64', (room, after)).fetchall()
            # 观察完整事件写审计；未来回忆使用当前可用记忆，避免撤销后的旧内容回流。
            public, cursor, used = [], after, 0
            for row in rows:
                event = {**dict(row), 'detail': json.loads(row['detail'])} if row['kind'] in PUBLIC_KINDS else None
                size = len(dump(event).encode('utf-8')) if event else 0
                if used + size > 60000:
                    break
                if event:
                    public.append(event)
                used += size
                cursor = row['seq']
            latest = db.execute('SELECT coalesce(max(seq),0) FROM active_events WHERE room=?', (room,)).fetchone()[0]
            result = {'room': room, 'role': owner['id'], 'cursor': cursor, 'more': cursor < latest,
                      'events': public, 'version': state['version'], 'memory_namespace': 'storyroom:' + room + ':' + self.ensure_runtime(db, room)['branch'],
                      'runtime': self.runtime_view(db, room),
                      'memory_revision': state['version'], 'talk': self.talk_view(db, room)}
            # 不每次下载全量记忆，实际回答由claim原子构造最新上下文。
            return result

    def answer_question(self, room, task, text):
        from .store import RoomError
        with self.db() as db:
            self.progress_guard(db, room, manual=True)
            row = db.execute("SELECT * FROM tasks WHERE id=? AND room=? AND status='proposed'", (task, room)).fetchone()
            state = self.room(db, room)
            if not row or json.loads(row['result']).get('intent') != 'question' or row['version'] != state['version']:
                raise RoomError('只能回复当前现场的待回答问题', 409)
            db.execute("UPDATE tasks SET status='answered' WHERE id=?", (task,))
            db.execute('UPDATE rooms SET version=version+1 WHERE id=?', (room,))
            db.execute("UPDATE tasks SET status='cancelled' WHERE room=? AND status IN ('queued','running')", (room,))
            db.execute('UPDATE room_talk SET opened=1,remaining=limit_turns,ready_at=? WHERE room=?', (self.clock() + 1, room))
            seq = self.event(db, room, '主持人叙事', '主持人', {'任务': task, '内容': text, '说明': '回复玩家问题，作为已确认现场；随后继续讨论'})
            self.remember_narration(db, room, seq, text)
        self.export(room)
        return {'ok': True}

    def extra_context(self, db, room, question):
        memory = self.memory_view(db, room)
        keys = {question[i:i+2].lower() for i in range(max(0, len(question)-1))}
        scored = sorted(enumerate(memory['notes']), key=lambda p: (
            sum(key in (p[1]['subject'] + p[1]['text']).lower() for key in keys), -p[0]), reverse=True)
        notes, used = [], 0
        for _, note in scored[:12]:
            size = len(note['text']) + len(note['subject'])
            if used + size > 12000:
                break
            notes.append(note)
            used += size
        dialogue = [dict(r) for r in db.execute("SELECT seq,actor,detail FROM active_events WHERE room=? AND kind IN ('玩家讨论','玩家提问','玩家交流','玩家提交提案') ORDER BY seq DESC LIMIT 16", (room,))]
        for row in dialogue:
            # 动作是设想，不作为已发生事实；不读取撤销记忆的旧事件文本。
            detail = json.loads(row.pop('detail'))
            row['speech'] = detail.get('发言', '')[:2000]
            row['intent'] = detail.get('意图', 'discussion')
        experiences = [dict(r) for r in db.execute('SELECT e.id,e.node,e.text,r.name AS player FROM experiences e JOIN roles r ON r.id=e.role WHERE e.room=? AND e.active=1 ORDER BY e.created DESC', (room,))]
        stage = self.room(db, room)['node']
        experiences.sort(key=lambda e: (sum(k in e['text'].lower() for k in keys), e['node'] == stage), reverse=True)
        return {'room_memory': notes, 'shared_experiences': experiences[:8], 'players': memory['players'], 'other_players': memory['other_players'],
                'conversation': list(reversed(dialogue)), 'memory_namespace': memory['namespace']}
